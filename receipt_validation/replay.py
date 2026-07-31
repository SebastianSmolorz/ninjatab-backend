"""Replay captured OCR responses through a post-processing pipeline and score
them against the labeller's ground truth.

Because the OCR responses are fixed, two pipelines compared here differ only in
their post-processing: the comparison is paired, and costs no API calls.
"""

import copy
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

from ninjatab.tabs.receipt_scanning.base import ScanContext
from ninjatab.tabs.receipt_scanning.postprocess import standard_post_process
from ninjatab.tabs.receipt_scanning.strategies import (
    _build_result_from_candidates,
    _postprocess_candidates,
)
from labeler.evaluation.labels import OCR_CAPTURES, load_labelled_cases
from labeler.evaluation.scorer import PRIORITY_WEIGHTS, rollup, score_against_label

# All 66 labelled cases, 5 runs x 3 calls each, stored with the labeller.
CAPTURE_DIR = OCR_CAPTURES

METRIC_KEYS = [
    "p1_item_totals_f1",
    "p1_item_count_exact",
    "p2_charges_f1",
    "charge_type_accuracy",
    "p3_self_reconciled",
    "p3_grand_total_correct",
    "p3_receipt_total_correct",
    "p4_adjustments_f1",
    "s_item_name",
    "s_establishment",
    "s_translated_name",
    "s_date_match",
    "s_currency_match",
]


def _ctx() -> ScanContext:
    """A context carrying only what post-processing reads (default currency)."""
    return ScanContext(
        image_bytes=b"", content_type="image/jpeg",
        default_currency="USD", tab_id="labelled-eval",
    )


def load_captures(capture_dir: Path = CAPTURE_DIR) -> dict[str, dict[int, list[dict]]]:
    """{case: {run: [call0, call1, ...]}} from disk."""
    captures: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for path in sorted(capture_dir.glob("*/*.json")):
        record = json.loads(path.read_text())
        captures[record["case"]][record["run"]].append(record)
    for runs in captures.values():
        for calls in runs.values():
            calls.sort(key=lambda r: r["call"])
    return {k: dict(v) for k, v in captures.items()}


# -- pipelines ---------------------------------------------------------------
# A pipeline takes the list of captured call records for one run and returns a
# post-processed annotation (or None).


def pipeline_baseline(records: list[dict]) -> Optional[dict]:
    """Production single-call behaviour: post-process the first response."""
    annotation = copy.deepcopy(records[0]["annotation"])
    if annotation is None:
        return None
    standard_post_process(annotation, "USD")
    return annotation


def pipeline_consensus(records: list[dict], n: int = 3) -> Optional[dict]:
    """Production concurrent_consensus behaviour over the run's N responses."""
    ocr_results = [
        {
            "annotation": copy.deepcopy(r["annotation"]),
            "parse_error": r["parse_error"],
            "ocr_markdown": r.get("markdown") or "",
            "ocr_pages": 1,
            "ocr_markdown_chars": len(r.get("markdown") or ""),
            "call_ms": r["call_ms"],
        }
        for r in records[:n]
    ]
    candidates, candidate_metrics = _postprocess_candidates(ocr_results, _ctx())
    result = _build_result_from_candidates(
        candidates, candidate_metrics, ocr_results, {}, n
    )
    return result.document_annotation


def pipeline_verified(records: list[dict]) -> Optional[dict]:
    """Single call, then the deterministic verify/repair stage."""
    from ninjatab.tabs.receipt_scanning.verify import verify_and_repair

    annotation = pipeline_baseline(records)
    if annotation is None:
        return None
    verify_and_repair(annotation, records[0].get("markdown") or "")
    return annotation


def pipeline_verified_consensus(records: list[dict], n: int = 3) -> Optional[dict]:
    """Consensus selection, then the verify/repair stage on the winner."""
    from ninjatab.tabs.receipt_scanning.verify import verify_and_repair

    annotation = pipeline_consensus(records, n)
    if annotation is None:
        return None
    markdown = "\n".join(r.get("markdown") or "" for r in records[:n])
    verify_and_repair(annotation, markdown)
    return annotation


def pipeline_repair_then_select(records: list[dict], n: int = 3) -> Optional[dict]:
    """Repair each candidate *before* consensus selection, so the selector
    chooses among reconciled parses rather than picking one and repairing after.
    """
    from ninjatab.tabs.receipt_scanning.verify import verify_and_repair

    ocr_results = [
        {
            "annotation": copy.deepcopy(r["annotation"]),
            "parse_error": r["parse_error"],
            "ocr_markdown": r.get("markdown") or "",
            "ocr_pages": 1,
            "ocr_markdown_chars": len(r.get("markdown") or ""),
            "call_ms": r["call_ms"],
        }
        for r in records[:n]
    ]
    candidates, candidate_metrics = _postprocess_candidates(ocr_results, _ctx())
    for candidate, record in zip(candidates, records[:n]):
        if candidate is not None:
            verify_and_repair(candidate, record.get("markdown") or "")
    result = _build_result_from_candidates(
        candidates, candidate_metrics, ocr_results, {}, n
    )
    annotation = result.document_annotation
    if annotation is not None:
        verify_and_repair(annotation, records[0].get("markdown") or "")
    return annotation


def pipeline_ledger(records: list[dict]) -> Optional[dict]:
    """Single call through the ledger reconciliation."""
    from ninjatab.tabs.receipt_scanning.ledger import reconcile_ledger

    annotation = copy.deepcopy(records[0]["annotation"])
    if annotation is None:
        return None
    reconcile_ledger(annotation, "USD")
    return annotation


def pipeline_ledger_consensus(records: list[dict], n: int = 3) -> Optional[dict]:
    """Ledger reconciliation on each candidate, then consensus, then surplus
    repair — the full replacement composition."""
    from ninjatab.tabs.receipt_scanning.ledger import reconcile_ledger
    from ninjatab.tabs.receipt_scanning.verify import verify_and_repair

    candidates, candidate_metrics = [], []
    ocr_results = []
    for r in records[:n]:
        annotation = copy.deepcopy(r["annotation"])
        ocr_results.append({
            "annotation": annotation,
            "parse_error": r["parse_error"],
            "ocr_markdown": r.get("markdown") or "",
            "ocr_pages": 1,
            "ocr_markdown_chars": len(r.get("markdown") or ""),
            "call_ms": r["call_ms"],
        })
        if annotation is None:
            candidates.append(None)
            candidate_metrics.append(None)
            continue
        candidate_metrics.append(reconcile_ledger(annotation, "USD"))
        verify_and_repair(annotation)
        candidates.append(annotation)

    result = _build_result_from_candidates(
        candidates, candidate_metrics, ocr_results, {}, n
    )
    annotation = result.document_annotation
    if annotation is not None:
        verify_and_repair(annotation)
    return annotation


def pipeline_strategy(name: str, n: int = 3, flatten: bool = False):
    """Drive a real registered strategy's `post_process` over captured OCR, so
    the shipped class is what gets measured rather than a replay of its logic.

    `flatten` applies the v1 presenter, measuring what the shipped mobile client
    actually receives rather than the ledger the v2 endpoint returns.
    """

    def run(records: list[dict]) -> Optional[dict]:
        from ninjatab.tabs.receipt_scanning.ledger import flatten_for_v1
        from ninjatab.tabs.receipt_scanning.strategies import STRATEGIES_BY_NAME

        strategy = STRATEGIES_BY_NAME[name]
        ocr_results = [
            {
                "annotation": copy.deepcopy(r["annotation"]),
                "parse_error": r["parse_error"],
                "ocr_markdown": r.get("markdown") or "",
                "ocr_pages": 1,
                "ocr_markdown_chars": len(r.get("markdown") or ""),
                "call_ms": r["call_ms"],
            }
            for r in records[:n]
        ]
        annotation = strategy.post_process(ocr_results, _ctx()).document_annotation
        return flatten_for_v1(annotation) if flatten else annotation

    return run


PIPELINES: dict[str, Callable[[list[dict]], Optional[dict]]] = {
    "baseline": pipeline_baseline,
    "consensus": pipeline_consensus,
    "verified": pipeline_verified,
    "verified_consensus": pipeline_verified_consensus,
    "repair_then_select": pipeline_repair_then_select,
    "ledger": pipeline_ledger,
    "ledger_consensus": pipeline_ledger_consensus,
    # The production classes themselves, exercised end-to-end over the captures.
    "strategy:concurrent_consensus": pipeline_strategy("concurrent_consensus"),
    "strategy:verified_consensus": pipeline_strategy("verified_consensus"),
    # The same scan as seen by each endpoint: v2 gets the ledger, v1 the flat
    # list. Scoring both is how we check the presenter costs nothing.
    "v1:verified_consensus": pipeline_strategy("verified_consensus", flatten=True),
}


def register(name: str, fn: Callable[[list[dict]], Optional[dict]]) -> None:
    PIPELINES[name] = fn


# -- evaluation --------------------------------------------------------------


def evaluate(
    pipeline: str,
    captures: Optional[dict] = None,
    labels: Optional[dict] = None,
) -> dict:
    """Score `pipeline` over every case and run. Returns per-case, per-run
    metrics plus aggregates and a stability measure."""
    captures = captures if captures is not None else load_captures()
    labels = labels if labels is not None else load_labelled_cases()
    fn = PIPELINES[pipeline]

    per_run: list[dict] = []
    per_case: dict[str, list[dict]] = defaultdict(list)
    signatures: dict[str, set] = defaultdict(set)

    for case, runs in sorted(captures.items()):
        label = labels.get(case)
        if label is None:
            continue
        annotation = label["annotation"]
        for run in sorted(runs):
            try:
                result = fn(runs[run])
            except Exception:  # noqa: BLE001 - a crashing pipeline scores as a failure
                result = None
            metrics = score_against_label(result, annotation)
            metrics["case"] = case
            metrics["run"] = run
            metrics["rollup"] = rollup(metrics)
            per_run.append(metrics)
            per_case[case].append(metrics)
            signatures[case].add(_signature(result))

    return {
        "pipeline": pipeline,
        "per_run": per_run,
        "per_case": dict(per_case),
        "aggregate": _aggregate(per_run),
        "stability": _stability(per_case, signatures),
    }


def _signature(result: Optional[dict]) -> str:
    """A canonical form of the financially meaningful output, used to count how
    many distinct answers a case produced across runs."""
    if not result:
        return "FAILED"
    items = sorted(
        f"{(i.get('category') or 'item')}:{i.get('total')}"
        for i in result.get("items") or []
    )
    # Two candidates identical in their items but differing in what was charged
    # on top are different answers, so the adjustments belong in the signature.
    adjustments = sorted(
        f"{a.get('type')}:{a.get('amount')}"
        for a in result.get("adjustments") or []
    )
    return json.dumps(
        {
            "items": items,
            "adjustments": adjustments,
            "receipt_total": str(result.get("receipt_total")),
            "tax": str(result.get("tax")),
            "tip": str(result.get("tip")),
            "service_charge": str(result.get("service_charge")),
        },
        sort_keys=True,
    )


def _mean(values: list) -> Optional[float]:
    numeric = [
        (1.0 if v is True else 0.0 if v is False else float(v))
        for v in values if v is not None
    ]
    return sum(numeric) / len(numeric) if numeric else None


def _aggregate(per_run: list[dict]) -> dict:
    out = {}
    for key in METRIC_KEYS + ["rollup"]:
        out[key] = _mean([m.get(key) for m in per_run])
    out["n_observations"] = len(per_run)
    out["failures"] = sum(1 for m in per_run if m["failed"])
    return out


def _stability(per_case: dict, signatures: dict) -> dict:
    """How consistent repeated runs of the same case are."""
    distinct = {case: len(sigs) for case, sigs in signatures.items()}
    unstable = {c: n for c, n in distinct.items() if n > 1}
    rollup_spread = {}
    for case, runs in per_case.items():
        values = [m["rollup"] for m in runs if m["rollup"] is not None]
        if len(values) > 1:
            rollup_spread[case] = max(values) - min(values)
    stdevs = [
        statistics.pstdev([m["rollup"] for m in runs if m["rollup"] is not None])
        for runs in per_case.values()
        if len([m for m in runs if m["rollup"] is not None]) > 1
    ]
    return {
        "cases": len(distinct),
        "fully_consistent": sum(1 for n in distinct.values() if n == 1),
        "unstable_cases": unstable,
        "distinct_answers": distinct,
        "mean_rollup_spread": _mean(list(rollup_spread.values())),
        "max_rollup_spread": max(rollup_spread.values()) if rollup_spread else None,
        "rollup_stdev": statistics.mean(stdevs) if stdevs else None,
    }


def format_report(report: dict, baseline: Optional[dict] = None) -> str:
    agg = report["aggregate"]
    lines = [f"=== {report['pipeline']} ===",
             f"observations: {agg['n_observations']}  failures: {agg['failures']}"]
    for key in METRIC_KEYS + ["rollup"]:
        value = agg[key]
        cell = "  n/a" if value is None else f"{value:.4f}"
        delta = ""
        if baseline is not None:
            base_value = baseline["aggregate"].get(key)
            if value is not None and base_value is not None:
                diff = value - base_value
                delta = f"   ({diff:+.4f})"
        weight = PRIORITY_WEIGHTS.get(key)
        tag = f" [w{weight}]" if weight else ""
        lines.append(f"  {key:<28} {cell}{delta}{tag}")
    stab = report["stability"]
    lines.append(
        f"  stability: {stab['fully_consistent']}/{stab['cases']} cases identical across runs; "
        f"mean rollup stdev {stab['rollup_stdev']:.4f}"
        if stab["rollup_stdev"] is not None else "  stability: n/a"
    )
    return "\n".join(lines)
