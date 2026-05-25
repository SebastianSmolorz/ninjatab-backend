"""Concrete receipt scanning strategies and their registry."""

from concurrent.futures import ThreadPoolExecutor

from .base import (
    ReceiptScanStrategy,
    ScanContext,
    ScanResult,
    mistral_client,
    parse_receipt_date,
    run_single_ocr,
)
from .postprocess import (
    _items_receipt_gap,
    _to_float,
    field_consensus,
    most_common_consensus,
    recompute_total_match,
    select_best_line_items,
    standard_post_process,
)
from .sources import default_ref


class BaselineStrategy(ReceiptScanStrategy):
    """Current production behaviour: one OCR call, standard post-processing."""

    name = "baseline_mistral_ocr"


class ConcurrentConsensusStrategy(ReceiptScanStrategy):
    """Fire N concurrent OCR requests on the same image, post-process each
    candidate, then combine them:

    - scalar fields (currency, establishment, date, receipt_total) are taken as
      the modal value across runs, removing per-run noise;
    - line items are taken whole from the single best candidate, chosen by the
      items_total-vs-receipt_total gap but tie-broken toward the modal item
      count so a run that merged rows to hit the total does not win.

    Falls back to most-common-field / first when no candidate has a usable
    receipt_total. The Mistral OCR API does not dedupe identical images
    (verified), so all N requests reuse a single image reference."""

    name = "concurrent_consensus"

    def __init__(self, n_requests: int = 3):
        self.n_requests = n_requests

    def pre_process(self, ctx: ScanContext) -> list[str]:
        return [default_ref(ctx)] * self.n_requests

    def call_mistral(self, prepared: list[str], ctx: ScanContext) -> list[dict]:
        client = mistral_client()
        with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
            return list(pool.map(
                lambda url: run_single_ocr(client, url, self.prompt, self.model),
                prepared,
            ))

    def post_process(self, ocr_results: list[dict], ctx: ScanContext) -> ScanResult:
        # Post-process each candidate independently so its items_total reflects
        # what the user would actually get for that run.
        candidates: list[dict] = []
        candidate_metrics: list[dict] = []
        for ocr in ocr_results:
            ann = ocr["annotation"]
            if ann is None:
                candidates.append(None)
                candidate_metrics.append(None)
                continue
            pm = standard_post_process(ann, ctx.default_currency)
            candidates.append(ann)
            candidate_metrics.append(pm)

        method = "best_line_items"
        idx = select_best_line_items(candidates)
        if idx is None:
            idx = most_common_consensus(candidates)
            method = "most_common" if idx is not None else "none"

        metrics = self.base_metrics(ctx)
        metrics.update({
            "consensus_n_requested": self.n_requests,
            "consensus_n_candidates": sum(1 for c in candidates if c is not None),
            "consensus_selection_method": method,
            "consensus_selected_index": idx,
            "consensus_candidate_items_totals": [
                _to_float((c or {}).get("items_total")) for c in candidates
            ],
            "consensus_candidate_receipt_totals": [
                _to_float((c or {}).get("receipt_total")) for c in candidates
            ],
            "consensus_candidate_item_counts": [
                len((c or {}).get("items") or []) if c else None for c in candidates
            ],
            "consensus_candidate_gaps": [
                _items_receipt_gap(c) if c else None for c in candidates
            ],
        })

        if idx is None:
            metrics["annotation_parse_error"] = all(o["parse_error"] for o in ocr_results)
            metrics["consensus_selected_gap"] = None
            return ScanResult(document_annotation=None, date=parse_receipt_date(None)[0], metrics=metrics)

        chosen_ocr = ocr_results[idx]
        chosen = candidates[idx]
        metrics["ocr_pages"] = chosen_ocr["ocr_pages"]
        metrics["ocr_markdown_chars"] = chosen_ocr["ocr_markdown_chars"]
        metrics["annotation_parse_error"] = chosen_ocr["parse_error"]
        metrics.update(candidate_metrics[idx])

        # Overlay the modal scalar fields, then recompute the totals match
        # against the (possibly updated) receipt_total.
        consensus_fields = field_consensus(candidates)
        overridden = {}
        for key, value in consensus_fields.items():
            if value is not None and chosen.get(key) != value:
                overridden[key] = {"from": chosen.get(key), "to": value}
                chosen[key] = value
        metrics["consensus_overridden_fields"] = list(overridden)
        metrics.update(recompute_total_match(chosen))
        metrics["consensus_selected_gap"] = _items_receipt_gap(chosen)

        date_str, parsed = parse_receipt_date(chosen)
        metrics["date_parsed"] = parsed
        return ScanResult(document_annotation=chosen, date=date_str, metrics=metrics)


class EscalatingStrategy(ReceiptScanStrategy):
    """Run a cheap single-call strategy first; only escalate to a more expensive
    strategy when the result is not trustworthy - i.e. the items_total does not
    reconcile with the receipt_total (the proxy for a correct parse), or no
    annotation came back at all. Keeps the common case realtime/single-call and
    spends extra OCR calls only on receipts that demonstrably need them."""

    name = "escalating"

    def __init__(self, base: ReceiptScanStrategy = None, escalate_to: ReceiptScanStrategy = None):
        self.base = base or BaselineStrategy()
        self.escalate_to = escalate_to or ConcurrentConsensusStrategy()

    def run(self, ctx: ScanContext) -> ScanResult:
        first = self.base.run(ctx)
        reconciled = first.metrics.get("items_match_receipt_total") is True

        if reconciled:
            first.metrics["strategy"] = self.name
            first.metrics["escalated"] = False
            first.metrics["escalation_reason"] = None
            first.metrics["escalation_base_strategy"] = self.base.name
            return first

        reason = "no_annotation" if first.document_annotation is None else "totals_unreconciled"
        second = self.escalate_to.run(ctx)
        second.metrics["strategy"] = self.name
        second.metrics["escalated"] = True
        second.metrics["escalation_reason"] = reason
        second.metrics["escalation_base_strategy"] = self.base.name
        second.metrics["escalation_escalated_to"] = self.escalate_to.name

        base_ms = first.timings.get("total_ms", 0)
        esc_ms = second.timings.get("total_ms", 0)
        second.timings = {
            "total_ms": base_ms + esc_ms,
            "base_ms": base_ms,
            "escalated_ms": esc_ms,
            "mistral_ms": (first.timings.get("mistral_ms") or 0) + (second.timings.get("mistral_ms") or 0),
        }
        second.metrics["scan_total_ms"] = second.timings["total_ms"]
        return second


STRATEGIES = [BaselineStrategy(), ConcurrentConsensusStrategy(), EscalatingStrategy()]
STRATEGIES_BY_NAME = {s.name: s for s in STRATEGIES}
DEFAULT_STRATEGY = "baseline_mistral_ocr"
