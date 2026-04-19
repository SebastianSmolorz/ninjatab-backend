import base64
import json
import mimetypes
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from django.conf import settings
from mistralai import Mistral, ImageURLChunk
from mistralai.extra import response_format_from_pydantic_model

from ninjatab.tabs.receipt_service import _Document
from receipt_validation.scorer import score_result

CASES_DIR = Path(__file__).parent / "cases"


def _load_cases(case_uuids: Optional[list[str]] = None) -> dict[str, dict]:
    """Return {uuid: {"image_path": Path, "expected": dict}} for each case."""
    cases = {}
    for case_dir in sorted(CASES_DIR.iterdir()):
        if not case_dir.is_dir():
            continue
        uuid = case_dir.name
        if case_uuids and uuid not in case_uuids:
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
        cases[uuid] = {"image_path": image_path, "expected": expected}
    return cases


def _run_mistral_ocr(strategy: dict, image_path: Path, case_uuid: str) -> dict:
    content_type, _ = mimetypes.guess_type(str(image_path))
    content_type = content_type or "image/jpeg"
    image_b64 = base64.b64encode(image_path.read_bytes()).decode()
    image_url = f"data:{content_type};base64,{image_b64}"

    client = Mistral(api_key=settings.MISTRAL_API_KEY)
    response = client.ocr.process(
        model="mistral-ocr-latest",
        document=ImageURLChunk(image_url=image_url),
        document_annotation_format=response_format_from_pydantic_model(_Document),
        document_annotation_prompt=strategy["prompt"],
        include_image_base64=False,
    )
    annotation = None
    raw = response.document_annotation
    if raw and isinstance(raw, str) and not raw.startswith("~?~"):
        annotation = json.loads(raw)
    return {"document_annotation": annotation}


def run_strategy(strategy: dict, image_path: Path, case_uuid: str) -> dict:
    """Blackbox: run a strategy on a local image and return a result dict."""
    api = strategy["api"]
    if api == "mistral_ocr":
        return _run_mistral_ocr(strategy, image_path, case_uuid)
    raise ValueError(f"Unknown api: {api!r}")


def _mean(values: list) -> Optional[float]:
    filtered = [v for v in values if v is not None]
    return sum(filtered) / len(filtered) if filtered else None


def run_pipeline(
    case_uuids: Optional[list[str]],
    strategy_names: Optional[list[str]],
    runs_per_strategy: int,
    strategies: list[dict],
) -> dict:
    from receipt_validation.strategies import STRATEGIES_BY_NAME

    cases = _load_cases(case_uuids)
    selected_strategies = (
        [STRATEGIES_BY_NAME[n] for n in strategy_names if n in STRATEGIES_BY_NAME]
        if strategy_names
        else strategies
    )

    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "cases": {},
    }

    for case_uuid, case_data in cases.items():
        image_path = case_data["image_path"]
        expected = case_data["expected"]
        establishment = (expected.get("document_annotation") or {}).get("receipt_establishment_name")
        case_out: dict = {
            "image_path": str(image_path.relative_to(Path(__file__).parent.parent)),
            "receipt_establishment_name": establishment,
            "strategies": {},
        }

        for strategy in selected_strategies:
            strategy_name = strategy["name"]
            runs_out = []

            for run_idx in range(1, runs_per_strategy + 1):
                error = None
                result = None
                scores = None
                try:
                    result = run_strategy(strategy, image_path, case_uuid)
                    scores = score_result(result, expected)
                except Exception:
                    error = traceback.format_exc()

                runs_out.append({
                    "run": run_idx,
                    "result": result,
                    "scores": scores,
                    "error": error,
                })

            score_keys = ["total_score", "receipt_total_accuracy", "items_total_accuracy", "item_count_match", "items_sum_vs_receipt_total", "items_sum_vs_items_total", "item_name_fuzzy_match", "item_translated_name_fuzzy_match", "currency_match", "language_match", "date_match"]
            aggregate = {
                f"mean_{k}": _mean([r["scores"][k] for r in runs_out if r["scores"]])
                for k in score_keys
            }
            aggregate["success_rate"] = sum(1 for r in runs_out if r["error"] is None) / runs_per_strategy

            case_out["strategies"][strategy_name] = {
                "runs": runs_out,
                "aggregate": aggregate,
            }

        output["cases"][case_uuid] = case_out

    return output
