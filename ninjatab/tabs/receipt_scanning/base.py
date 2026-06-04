"""Base orchestration for receipt scanning strategies.

A strategy's `run()` drives three swappable stages — pre-Mistral, the Mistral
call, and post-processing — and records per-stage timing. Subclasses override
the stage methods, not `run()`.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from typing import Optional

import sentry_sdk
from django.conf import settings
from django.utils import timezone
from mistralai.client import Mistral
from mistralai.client.models import ImageURLChunk
from mistralai.extra import response_format_from_pydantic_model

from .postprocess import standard_post_process
from .prompt import DOCUMENT_ANNOTATION_PROMPT
from .schema import _Document

logger = logging.getLogger("app")


@dataclass
class ScanContext:
    """Everything a strategy needs to scan one receipt, independent of how the
    image was acquired (prod upload vs. local validation file)."""
    image_bytes: bytes
    content_type: str
    default_currency: str
    tab_id: str
    s3_base_key: Optional[str] = None  # already-uploaded key; copy source for extra requests
    deskew_applied: bool = False  # guards against double-deskewing a reused ctx
    deskew_angle: Optional[float] = None  # detected skew angle, once deskew has run


@dataclass
class ScanResult:
    document_annotation: Optional[dict]
    date: str
    metrics: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)


def mistral_client() -> Mistral:
    return Mistral(api_key=settings.MISTRAL_API_KEY)


def run_single_ocr(client: Mistral, image_url: str, prompt: str, model: str) -> dict:
    """Fire one OCR call and return {"annotation": dict|None,
    "ocr_markdown": str, "parse_error": bool, "ocr_pages": int,
    "ocr_markdown_chars": int, "call_ms": int}. Malformed JSON is captured to
    Sentry, not raised."""
    started = timezone.now()
    response = client.ocr.process(
        model=model,
        document=ImageURLChunk(image_url=image_url),
        document_annotation_format=response_format_from_pydantic_model(_Document),
        document_annotation_prompt=prompt,
        timeout_ms=55_000,
    )
    call_ms = int((timezone.now() - started).total_seconds() * 1000)

    pages = response.pages or []
    ocr_markdown = "\n\n".join(p.markdown or "" for p in pages)
    print(ocr_markdown)

    annotation = None
    parse_error = False
    raw = response.document_annotation
    if raw and isinstance(raw, str) and not raw.startswith("~?~"):
        try:
            annotation = json.loads(raw)
            print(annotation)
        except json.JSONDecodeError as e:
            sentry_sdk.capture_exception(e, contexts={
                "mistral_ocr": {"raw_length": len(raw), "raw_preview": raw[:500]},
            })
            logger.warning("Mistral OCR returned malformed JSON: %s", e)
            parse_error = True

    return {
        "annotation": annotation,
        "ocr_markdown": ocr_markdown,
        "parse_error": parse_error,
        "ocr_pages": len(pages),
        "ocr_markdown_chars": len(ocr_markdown),
        "call_ms": call_ms,
    }


def parse_receipt_date(annotation: Optional[dict]) -> tuple[str, bool]:
    """Parse datetime_of_receipt to a YYYY-MM-DD string, defaulting to today.
    Returns (date_str, parsed)."""
    default = timezone.now().strftime("%Y-%m-%d")
    if not annotation or not annotation.get("datetime_of_receipt"):
        return default, False
    raw_dt = annotation["datetime_of_receipt"].strip()
    try:
        parsed = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d"), True
    except (ValueError, TypeError):
        try:
            return date_type.fromisoformat(raw_dt[:10]).isoformat(), True
        except (ValueError, TypeError):
            return default, False


class ReceiptScanStrategy:
    """Base strategy: single OCR call, standard post-processing. Subclass and
    override pre_process / call_mistral / post_process to vary behaviour."""

    name: str = "base"
    prompt: str = DOCUMENT_ANNOTATION_PROMPT
    model: str = "mistral-ocr-latest"
    version: str = "1"
    deskew: bool = True  # straighten the receipt text before OCR; skip via deskew=False

    def __init__(self, *, name=None, model=None, prompt=None, version=None, deskew=None):
        """Optionally override the class-level name/model/prompt/version/deskew on
        a per-instance basis. Unset arguments keep the class default, so existing
        zero-arg construction is unchanged. Used by the validation harness to
        define configurable strategy variants."""
        if name is not None:
            self.name = name
        if model is not None:
            self.model = model
        if prompt is not None:
            self.prompt = prompt
        if version is not None:
            self.version = version
        if deskew is not None:
            self.deskew = deskew

    def _maybe_deskew(self, ctx: ScanContext) -> None:
        """Straighten the receipt text in place, once per ScanContext. Rewrites
        ctx.image_bytes (and content_type) to the deskewed JPEG and drops
        s3_base_key so downstream references send the corrected bytes inline
        rather than the stale S3 original. No-op when disabled or already run."""
        if not self.deskew or ctx.deskew_applied:
            return
        # Imported lazily so the (heavy) cv2/numpy import is paid only when used.
        from .deskew import deskew_bytes

        deskewed, angle = deskew_bytes(ctx.image_bytes)
        ctx.deskew_applied = True
        ctx.deskew_angle = angle
        if deskewed is not ctx.image_bytes:
            ctx.image_bytes = deskewed
            ctx.content_type = "image/jpeg"
            ctx.s3_base_key = None  # S3 copy is now stale; force inline data URL
        logger.info("Deskew applied (%s): angle=%.3f°", self.name, angle)

    def base_metrics(self, ctx: ScanContext) -> dict:
        return {
            "strategy": self.name,
            "strategy_version": self.version,
            "model": self.model,
            "tab_id": ctx.tab_id,
            "tab_default_currency": ctx.default_currency,
            "annotation_present": False,
            "annotation_parse_error": False,
            "ocr_pages": 0,
            "ocr_markdown_chars": 0,
            "mistral_call_ms": None,
            "date_parsed": False,
            "deskew_applied": ctx.deskew_applied,
            "deskew_angle": ctx.deskew_angle,
        }

    def run(self, ctx: ScanContext) -> ScanResult:
        timings: dict = {}
        t0 = timezone.now()

        pre_t = timezone.now()
        prepared = self.pre_process(ctx)
        timings["pre_ms"] = int((timezone.now() - pre_t).total_seconds() * 1000)

        mistral_t = timezone.now()
        ocr_results = self.call_mistral(prepared, ctx)
        timings["mistral_ms"] = int((timezone.now() - mistral_t).total_seconds() * 1000)
        timings["per_call_ms"] = [r["call_ms"] for r in ocr_results]

        post_t = timezone.now()
        result = self.post_process(ocr_results, ctx)
        timings["post_ms"] = int((timezone.now() - post_t).total_seconds() * 1000)

        timings["total_ms"] = int((timezone.now() - t0).total_seconds() * 1000)
        result.timings = timings
        result.metrics["scan_total_ms"] = timings["total_ms"]
        result.metrics["mistral_call_ms"] = timings["mistral_ms"]
        return result

    # -- stages -----------------------------------------------------------

    def pre_process(self, ctx: ScanContext) -> list[str]:
        """Return the list of image references (URLs) to OCR. Default: one,
        after straightening the receipt text (unless deskew is disabled)."""
        from .sources import default_ref
        self._maybe_deskew(ctx)
        return [default_ref(ctx)]

    def call_mistral(self, prepared: list[str], ctx: ScanContext) -> list[dict]:
        """Run one OCR call per prepared reference, sequentially."""
        client = mistral_client()
        return [run_single_ocr(client, url, self.prompt, self.model) for url in prepared]

    def post_process(self, ocr_results: list[dict], ctx: ScanContext) -> ScanResult:
        """Default: standard post-processing on the single candidate."""
        metrics = self.base_metrics(ctx)
        ocr = ocr_results[0]
        metrics["ocr_pages"] = ocr["ocr_pages"]
        metrics["ocr_markdown_chars"] = ocr["ocr_markdown_chars"]
        metrics["annotation_parse_error"] = ocr["parse_error"]

        annotation = ocr["annotation"]
        if annotation:
            metrics.update(standard_post_process(annotation, ctx.default_currency))

        date_str, parsed = parse_receipt_date(annotation)
        metrics["date_parsed"] = parsed
        return ScanResult(document_annotation=annotation, date=date_str, metrics=metrics)
