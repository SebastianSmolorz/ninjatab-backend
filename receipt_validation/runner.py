"""Run a scanning strategy against a local receipt image.

Used by the `scan_image` management command (which the labeller shells out to)
and by anything else that wants a real scan of a file on disk rather than an
uploaded one.

Scoring lives with the labels, in `labeler.evaluation.scorer`.
"""

import mimetypes
import uuid
from pathlib import Path

from django.conf import settings

from ninjatab.tabs.receipt_scanning.base import ScanContext
from ninjatab.tabs.receipt_service import _s3_client


def _s3_configured() -> bool:
    return bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)


def _upload_validation_image(image_bytes: bytes, content_type: str, ext: str) -> str:
    """Upload a local image to S3 so multi-request strategies get the same
    anti-dedupe behaviour as production. Returns the object key."""
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
        "raw_responses": result.raw_responses,
    }
