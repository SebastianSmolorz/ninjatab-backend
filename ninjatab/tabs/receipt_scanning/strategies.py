"""Concrete receipt scanning strategies and their registry."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone

from .base import (
    ReceiptScanStrategy,
    ScanContext,
    ScanResult,
    mistral_client,
    parse_receipt_date,
    run_single_ocr,
)
from .postprocess import (
    _annotation_tolerance,
    _is_reconciled,
    _items_receipt_gap,
    _to_float,
    field_consensus,
    most_common_consensus,
    recompute_total_match,
    select_best_line_items,
    standard_post_process,
)
from .sources import default_ref


def _postprocess_candidates(ocr_results: list[dict], ctx: ScanContext) -> tuple[list, list]:
    """Run standard post-processing on each OCR result's annotation exactly once.
    Returns parallel lists (candidates, candidate_metrics) with None placeholders
    for results that produced no annotation."""
    candidates: list = []
    candidate_metrics: list = []
    for ocr in ocr_results:
        ann = ocr["annotation"]
        if ann is None:
            candidates.append(None)
            candidate_metrics.append(None)
            continue
        candidate_metrics.append(standard_post_process(ann, ctx.default_currency))
        candidates.append(ann)
    return candidates, candidate_metrics


def _build_result_from_candidates(
    candidates: list,
    candidate_metrics: list,
    ocr_results: list[dict],
    base_metrics: dict,
    n_requested: int,
) -> ScanResult:
    """Combine already-post-processed candidates into one ScanResult: modal
    scalar fields overlaid onto the best line-item candidate, totals recomputed.
    Shared by every consensus-style strategy."""
    metrics = dict(base_metrics)
    metrics.update({
        "consensus_n_requested": n_requested,
        "consensus_n_candidates": sum(1 for c in candidates if c is not None),
        "consensus_candidate_item_counts": [
            len((c or {}).get("items") or []) if c else None for c in candidates
        ],
        "consensus_candidate_items_totals": [
            _to_float((c or {}).get("items_total")) for c in candidates
        ],
        "consensus_candidate_receipt_totals": [
            _to_float((c or {}).get("receipt_total")) for c in candidates
        ],
        "consensus_candidate_gaps": [
            _items_receipt_gap(c) if c else None for c in candidates
        ],
    })

    method = "best_line_items"
    idx = select_best_line_items(candidates)
    if idx is None:
        idx = most_common_consensus(candidates)
        method = "most_common" if idx is not None else "none"
    metrics["consensus_selection_method"] = method
    metrics["consensus_selected_index"] = idx

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

    # Overlay modal scalar fields, then recompute totals against the (possibly
    # updated) receipt_total.
    overridden = {}
    for key, value in field_consensus(candidates).items():
        if value is not None and chosen.get(key) != value:
            overridden[key] = {"from": chosen.get(key), "to": value}
            chosen[key] = value
    metrics["consensus_overridden_fields"] = list(overridden)
    metrics.update(recompute_total_match(chosen))
    metrics["consensus_selected_gap"] = _items_receipt_gap(chosen)

    date_str, parsed = parse_receipt_date(chosen)
    metrics["date_parsed"] = parsed
    return ScanResult(document_annotation=chosen, date=date_str, metrics=metrics)


def _fire_concurrent(ref: str, n: int, prompt: str, model: str) -> list[dict]:
    """Fire n OCR calls on the same image reference concurrently."""
    client = mistral_client()
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(
            lambda _i: run_single_ocr(client, ref, prompt, model),
            range(n),
        ))


class BaselineStrategy(ReceiptScanStrategy):
    """Current production behaviour: one OCR call, standard post-processing."""

    name = "baseline_mistral_ocr"


class ConcurrentConsensusStrategy(ReceiptScanStrategy):
    """Fire N concurrent OCR requests on the same image, then combine: modal
    scalar fields + line items from the best (merge-proof) candidate. Flat cost
    of N calls regardless of difficulty; latency ~= one call. The Mistral OCR API
    does not dedupe identical images (verified), so all N reuse one reference."""

    name = "concurrent_consensus"

    def __init__(self, n_requests: int = 3, **kwargs):
        super().__init__(**kwargs)
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
        candidates, candidate_metrics = _postprocess_candidates(ocr_results, ctx)
        return _build_result_from_candidates(
            candidates, candidate_metrics, ocr_results, self.base_metrics(ctx), self.n_requests,
        )


def _candidates_agree(candidates: list) -> tuple[bool, str]:
    """Decide whether an initial batch is trustworthy enough to accept without
    escalating. Requires at least two candidates that agree on line-item
    *structure*, not merely on the total (a single reconciled run can still have
    merged rows). Returns (accept, reason)."""
    valid = [c for c in candidates if c]
    if len(valid) < 2:
        return False, "insufficient_candidates"

    has_receipt_total = any(_to_float(c.get("receipt_total")) is not None for c in valid)
    # When a receipt_total exists, only trust candidates that reconcile to it;
    # otherwise fall back to raw structural agreement.
    pool = [c for c in valid if _is_reconciled(c)] if has_receipt_total else valid
    if len(pool) < 2:
        return False, "too_few_reconciled"

    counts = Counter(len(c.get("items") or []) for c in pool)
    modal_count, n_modal = counts.most_common(1)[0]
    if n_modal < 2:
        return False, "item_count_disagreement"

    agreeing = [c for c in pool if len(c.get("items") or []) == modal_count]
    totals = [t for t in (_to_float(c.get("items_total")) for c in agreeing) if t is not None]
    if totals and (max(totals) - min(totals)) > _annotation_tolerance(agreeing[0]):
        return False, "items_total_disagreement"

    return True, "agreed"


class TieredConsensusStrategy(ReceiptScanStrategy):
    """Fire a small initial batch concurrently; accept it only when at least two
    candidates agree on line-item structure (and reconcile to the receipt_total
    when present). Otherwise escalate with a few more concurrent calls and pool
    ALL candidates - the initial calls are reused, not discarded.

    Cheapest option on clean receipts (initial calls, one-call latency) while
    still cross-checking line items; deeper evidence on hard receipts at the cost
    of a second sequential batch (~2x latency) only when needed."""

    name = "tiered_consensus"

    def __init__(self, initial: int = 2, escalate_extra: int = 2, **kwargs):
        super().__init__(**kwargs)
        self.initial = initial
        self.escalate_extra = escalate_extra

    def run(self, ctx: ScanContext) -> ScanResult:
        t0 = timezone.now()
        ref = default_ref(ctx)

        b1_t = timezone.now()
        batch = _fire_concurrent(ref, self.initial, self.prompt, self.model)
        batch1_ms = int((timezone.now() - b1_t).total_seconds() * 1000)
        candidates, candidate_metrics = _postprocess_candidates(batch, ctx)

        accept, reason = _candidates_agree(candidates)
        escalated = not accept
        batch2_ms = 0
        if escalated:
            b2_t = timezone.now()
            extra = _fire_concurrent(ref, self.escalate_extra, self.prompt, self.model)
            batch2_ms = int((timezone.now() - b2_t).total_seconds() * 1000)
            extra_cands, extra_metrics = _postprocess_candidates(extra, ctx)
            batch += extra
            candidates += extra_cands
            candidate_metrics += extra_metrics

        n_requested = self.initial + (self.escalate_extra if escalated else 0)
        result = _build_result_from_candidates(
            candidates, candidate_metrics, batch, self.base_metrics(ctx), n_requested,
        )
        result.metrics.update({
            "tier_escalated": escalated,
            "tier_decision_reason": reason,
            "tier_initial_calls": self.initial,
            "tier_total_calls": len(batch),
        })

        mistral_ms = batch1_ms + batch2_ms
        total_ms = int((timezone.now() - t0).total_seconds() * 1000)
        result.timings = {
            "total_ms": total_ms,
            "mistral_ms": mistral_ms,
            "batch1_ms": batch1_ms,
            "batch2_ms": batch2_ms if escalated else None,
            "per_call_ms": [o["call_ms"] for o in batch],
        }
        result.metrics["scan_total_ms"] = total_ms
        result.metrics["mistral_call_ms"] = mistral_ms
        return result


class EscalatingStrategy(ReceiptScanStrategy):
    """Run a cheap single-call strategy first; only escalate to a more expensive
    strategy when the result is not trustworthy - i.e. the items_total does not
    reconcile with the receipt_total (the proxy for a correct parse), or no
    annotation came back at all. Keeps the common case realtime/single-call and
    spends extra OCR calls only on receipts that demonstrably need them."""

    name = "escalating"

    def __init__(self, base: ReceiptScanStrategy = None, escalate_to: ReceiptScanStrategy = None, **kwargs):
        super().__init__(**kwargs)
        self.base = base or BaselineStrategy()
        self.escalate_to = escalate_to or ConcurrentConsensusStrategy()

    def run(self, ctx: ScanContext) -> ScanResult:
        first = self.base.run(ctx)
        reconciled = first.metrics.get("items_match_receipt_total") is True

        if reconciled:
            first.metrics["strategy"] = self.name
            first.metrics["strategy_version"] = self.version
            first.metrics["escalated"] = False
            first.metrics["escalation_reason"] = None
            first.metrics["escalation_base_strategy"] = self.base.name
            return first

        reason = "no_annotation" if first.document_annotation is None else "totals_unreconciled"
        second = self.escalate_to.run(ctx)
        second.metrics["strategy"] = self.name
        second.metrics["strategy_version"] = self.version
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


STRATEGIES = [
    BaselineStrategy(),
    ConcurrentConsensusStrategy(),
    TieredConsensusStrategy(),
    EscalatingStrategy(),
]
STRATEGIES_BY_NAME = {s.name: s for s in STRATEGIES}
DEFAULT_STRATEGY = "baseline_mistral_ocr"


def resolve_strategy(option_or_name=None) -> ReceiptScanStrategy:
    """Resolve a ReceiptScanStrategy from the single ``STRATEGIES`` registry.

    Accepts either a strategy name (``str``, used by the validation pipeline), an
    Option (whose ``value`` is the name and which is ignored when inactive, used
    by ``scan_receipt``), or ``None``. Falls back to
    ``settings.RECEIPT_SCAN_STRATEGY`` when the input does not resolve to a known
    strategy, and to the baseline strategy if that setting is itself unresolvable.
    """
    from django.conf import settings

    fallback_name = getattr(settings, "RECEIPT_SCAN_STRATEGY", DEFAULT_STRATEGY)
    fallback = STRATEGIES_BY_NAME.get(fallback_name) or STRATEGIES_BY_NAME[DEFAULT_STRATEGY]

    if option_or_name is None:
        return fallback
    if isinstance(option_or_name, str):
        return STRATEGIES_BY_NAME.get(option_or_name) or fallback
    option = option_or_name
    if not option.active:
        return fallback
    return STRATEGIES_BY_NAME.get(option.value) or fallback
