import json
import mimetypes
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from django.conf import settings

from ninjatab.tabs.receipt_scanning.base import ScanContext
from ninjatab.tabs.receipt_service import _s3_client
from receipt_validation.scorer import score_result

CASES_DIR = Path(__file__).parent / "cases"


def _load_cases(case_uuids: Optional[list[str]] = None) -> dict[str, dict]:
    """Return {uuid: {"image_path": Path, "expected": dict}} for each case."""
    cases = {}
    for case_dir in sorted(CASES_DIR.iterdir()):
        if not case_dir.is_dir():
            continue
        uuid_ = case_dir.name
        if case_uuids and uuid_ not in case_uuids:
            continue
        image_path = next(
            (
                p
                for p in case_dir.iterdir()
                if p.stem == "image" and p.suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic"}
            ),
            None,
        )
        expected_path = case_dir / "expected.json"
        if image_path is None or not expected_path.exists():
            continue
        with open(expected_path) as f:
            expected = json.load(f)
        # Accept both shapes: a bare annotation or one wrapped in
        # {"document_annotation": ...}. Normalise to the wrapped form so the
        # scorer and establishment lookup work uniformly.
        if "document_annotation" not in expected:
            expected = {"document_annotation": expected}
        establishment = (expected.get("document_annotation") or {}).get(
            "receipt_establishment_name"
        )
        cases[uuid_] = {
            "image_path": image_path,
            "expected": expected,
            "establishment": establishment,
        }
    return cases


def _s3_configured() -> bool:
    return bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)


def _upload_validation_image(image_bytes: bytes, content_type: str, ext: str) -> str:
    """Upload a local validation image to S3 so multi-request strategies get the
    same anti-dedupe behaviour as production. Returns the object key."""
    key = f"receipts/validation/{uuid.uuid4()}.{ext}"
    _s3_client().put_object(
        Bucket=settings.S3_BUCKET,
        Key=key,
        Body=image_bytes,
        ACL="private",
        ContentType=content_type,
    )
    return key


def _build_context(image_path: Path, default_currency: str = "USD") -> ScanContext:
    content_type, _ = mimetypes.guess_type(str(image_path))
    content_type = content_type or "image/jpeg"
    image_bytes = image_path.read_bytes()
    s3_base_key = None
    if _s3_configured():
        ext = image_path.suffix.lstrip(".") or "jpg"
        s3_base_key = _upload_validation_image(image_bytes, content_type, ext)
    return ScanContext(
        image_bytes=image_bytes,
        content_type=content_type,
        default_currency=default_currency,
        tab_id="validation",
        s3_base_key=s3_base_key,
    )


def run_strategy(strategy, image_path: Path) -> dict:
    """Run a strategy class on a local image and return its post-processed
    result, timings and metrics."""
    ctx = _build_context(image_path)
    result = strategy.run(ctx)
    return {
        "document_annotation": result.document_annotation,
        "timings": result.timings,
        "metrics": result.metrics,
    }


def _run_case(strategy, case_uuid: str, case_data: dict, run_idx: int) -> tuple[str, dict]:
    """Run one strategy against one case, capturing any error. Safe to call from
    a worker thread: it shares no mutable state (clients are created per call).
    Returns (case_uuid, run record)."""
    error = result = scores = timings = None
    try:
        result = run_strategy(strategy, case_data["image_path"])
        timings = result.get("timings")
        scores = score_result(result, case_data["expected"])
    except Exception:
        error = traceback.format_exc()
    return case_uuid, {
        "run": run_idx,
        "result": result,
        "scores": scores,
        "timings": timings,
        "error": error,
    }


def _stdev(values: list) -> Optional[float]:
    filtered = [v for v in values if v is not None]
    if len(filtered) < 2:
        return None
    mean = sum(filtered) / len(filtered)
    return (sum((v - mean) ** 2 for v in filtered) / len(filtered)) ** 0.5


def _mean(values: list) -> Optional[float]:
    filtered = [v for v in values if v is not None]
    return sum(filtered) / len(filtered) if filtered else None


def _percentile(values: list, pct: float) -> Optional[float]:
    filtered = sorted(v for v in values if v is not None)
    if not filtered:
        return None
    k = (len(filtered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(filtered) - 1)
    return filtered[lo] + (filtered[hi] - filtered[lo]) * (k - lo)


def run_pipeline(
    case_uuids: Optional[list[str]],
    strategy_names: Optional[list[str]],
    runs_per_strategy: int,
    strategies: list,
    sleep_between_runs: int = 0,
    concurrency: int = 0,
) -> dict:
    from receipt_validation.variants import VARIANTS_BY_NAME

    cases = _load_cases(case_uuids)
    selected_strategies = (
        [VARIANTS_BY_NAME[n] for n in strategy_names if n in VARIANTS_BY_NAME]
        if strategy_names
        else strategies
    )

    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "cases": {},
    }

    # Initialise output structure
    for case_uuid, case_data in cases.items():
        output["cases"][case_uuid] = {
            "image_path": str(case_data["image_path"].relative_to(Path(__file__).parent.parent)),
            "receipt_establishment_name": case_data["establishment"],
            "strategies": {
                s.name: {"version": s.version, "model": s.model, "runs": []}
                for s in selected_strategies
            },
        }

    # strategy → run → case, so output groups by "strategy — run N" while the
    # per-run sleep still spreads repeat runs of each case apart in time. Within
    # a run the cases are fanned out concurrently (each scan is network-bound).
    max_workers = concurrency or len(cases) or 1
    for strategy in selected_strategies:
        for run_idx in range(1, runs_per_strategy + 1):
            if run_idx > 1 and sleep_between_runs > 0:
                time.sleep(sleep_between_runs)
            print(f"\n{strategy.name} v{strategy.version} — run {run_idx}", flush=True)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [
                    pool.submit(_run_case, strategy, case_uuid, case_data, run_idx)
                    for case_uuid, case_data in cases.items()
                ]
                # Consume as_completed on the main thread: prints and appends are
                # serialised here, so no locking is needed.
                for fut in as_completed(futures):
                    case_uuid, record = fut.result()
                    label = cases[case_uuid]["establishment"] or case_uuid
                    if record["error"]:
                        print(f"  {label:<28} ERR", flush=True)
                    else:
                        wait_ms = (record["timings"] or {}).get("total_ms")
                        status = f"ok   {wait_ms} ms" if wait_ms is not None else "ok"
                        print(f"  {label:<28} {status}", flush=True)
                    output["cases"][case_uuid]["strategies"][strategy.name]["runs"].append(record)

    # Compute aggregates after all runs complete
    score_keys = ["total_score", "receipt_total_accuracy", "items_total_accuracy", "item_count_match", "items_sum_vs_receipt_total", "items_sum_vs_items_total", "item_name_fuzzy_match", "item_translated_name_fuzzy_match", "currency_match", "language_match", "date_match"]
    for case_uuid, case_out in output["cases"].items():
        for strategy_name, strategy_out in case_out["strategies"].items():
            runs_out = strategy_out["runs"]
            aggregate = {
                f"mean_{k}": _mean([r["scores"][k] for r in runs_out if r["scores"]])
                for k in score_keys
            }
            aggregate["success_rate"] = sum(1 for r in runs_out if r["error"] is None) / runs_per_strategy
            total_scores = [r["scores"]["total_score"] for r in runs_out if r["scores"]]
            sd = _stdev(total_scores)
            aggregate["stability_score"] = max(0.0, 1.0 - sd) if sd is not None else None

            # "Blocking" latency = wall-clock total_ms: pre + actual API work
            # (concurrent calls counted once, sequential distinct calls summed)
            # + post. This is the real time the user waits per scan.
            total_ms = [r["timings"].get("total_ms") for r in runs_out if r["timings"]]
            mistral_ms = [r["timings"].get("mistral_ms") for r in runs_out if r["timings"]]
            aggregate["blocking_mean_ms"] = _mean(total_ms)
            aggregate["blocking_p50_ms"] = _percentile(total_ms, 0.50)
            aggregate["blocking_p95_ms"] = _percentile(total_ms, 0.95)
            aggregate["api_mean_ms"] = _mean(mistral_ms)  # diagnostic only
            strategy_out["aggregate"] = aggregate
    print()

    return output
