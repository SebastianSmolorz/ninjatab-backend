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
    most_common_consensus,
    select_closest_to_receipt_total,
    standard_post_process,
)
from .sources import default_ref


class BaselineStrategy(ReceiptScanStrategy):
    """Current production behaviour: one OCR call, standard post-processing."""

    name = "baseline_mistral_ocr"


class ConcurrentConsensusStrategy(ReceiptScanStrategy):
    """Fire N concurrent OCR requests on the same image, post-process each
    candidate, then keep the candidate whose calculated items_total is closest
    to its receipt_total. Falls back to most-common-field / first when no
    candidate has a receipt_total.

    Note: the Mistral OCR API does NOT dedupe identical images - repeated calls
    on the very same presigned URL return independently-varying output (verified
    empirically against the validation cases). So all N requests reuse a single
    image reference; no per-request copies/keys are needed."""

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

        method = "none"
        idx = select_closest_to_receipt_total(candidates)
        if idx is not None:
            method = "closest_total"
        else:
            idx = most_common_consensus(candidates)
            if idx is not None:
                method = "most_common"

        metrics = self.base_metrics(ctx)
        n_candidates = sum(1 for c in candidates if c is not None)
        metrics.update({
            "consensus_n_requested": self.n_requests,
            "consensus_n_candidates": n_candidates,
            "consensus_selection_method": method,
            "consensus_selected_index": idx,
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

        if idx is None:
            # Every request failed to produce an annotation.
            metrics["annotation_parse_error"] = all(o["parse_error"] for o in ocr_results)
            metrics["consensus_selected_gap"] = None
            return ScanResult(document_annotation=None, date=parse_receipt_date(None)[0], metrics=metrics)

        chosen_ocr = ocr_results[idx]
        chosen_ann = candidates[idx]
        metrics["ocr_pages"] = chosen_ocr["ocr_pages"]
        metrics["ocr_markdown_chars"] = chosen_ocr["ocr_markdown_chars"]
        metrics["annotation_parse_error"] = chosen_ocr["parse_error"]
        metrics.update(candidate_metrics[idx])
        metrics["consensus_selected_gap"] = _items_receipt_gap(chosen_ann)

        date_str, parsed = parse_receipt_date(chosen_ann)
        metrics["date_parsed"] = parsed
        return ScanResult(document_annotation=chosen_ann, date=date_str, metrics=metrics)


STRATEGIES = [BaselineStrategy(), ConcurrentConsensusStrategy()]
STRATEGIES_BY_NAME = {s.name: s for s in STRATEGIES}
DEFAULT_STRATEGY = "baseline_mistral_ocr"
