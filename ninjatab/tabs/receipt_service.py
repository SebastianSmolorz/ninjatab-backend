import logging
import uuid

import boto3
import sentry_sdk
from django.conf import settings
from django.db.models import F

# Pure receipt-parsing pieces now live in the receipt_scanning package. Re-export
# the schema and prompt here so existing imports keep working.
from ninjatab.tabs.receipt_scanning.prompt import DOCUMENT_ANNOTATION_PROMPT  # noqa: F401
from ninjatab.tabs.receipt_scanning.schema import (  # noqa: F401
    _Document,
    _Item,
    _OtherCharge,
)

logger = logging.getLogger("app")

MAX_SCANS_PER_TAB = 150

ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "image/heic", "image/heif", "application/octet-stream",
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


class ScanLimitExceeded(Exception):
    pass


def check_scan_limit(tab):
    """Check if tab has exceeded the receipt scan limit."""
    if tab.receipt_scan_count >= MAX_SCANS_PER_TAB:
        sentry_sdk.capture_message(
            f"Receipt scan limit reached for tab {tab.uuid} "
            f"({tab.receipt_scan_count} scans)",
            level="warning",
        )
        raise ScanLimitExceeded(
            f"Scan limit of {MAX_SCANS_PER_TAB} receipts per tab reached"
        )


def increment_scan_count(tab):
    """Increment the receipt scan count on the tab."""
    from ninjatab.tabs.models import Tab
    Tab.objects.filter(pk=tab.pk).update(receipt_scan_count=F('receipt_scan_count') + 1)


def validate_upload(file):
    """Validate file type and size. Raises ValueError on failure."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError(
            f"Unsupported file type: {file.content_type}. "
            "Allowed: JPEG, PNG, WebP, HEIC"
        )
    if file.size > MAX_UPLOAD_SIZE:
        raise ValueError(
            f"File too large. Maximum size is "
            f"{MAX_UPLOAD_SIZE // (1024 * 1024)} MB"
        )


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


def upload_to_spaces(file, tab_id: str) -> str:
    """Upload file to S3-compatible storage (private) and return the object key."""
    ext = file.name.rsplit(".", 1)[-1] if "." in file.name else "jpg"
    key = f"receipts/{tab_id}/{uuid.uuid4()}.{ext}"

    _s3_client().upload_fileobj(
        file,
        settings.S3_BUCKET,
        key,
        ExtraArgs={"ACL": "private", "ContentType": file.content_type},
    )
    return key


def generate_presigned_url(key: str, expiry: int = 3600) -> str:
    """Generate a pre-signed URL for a private S3 object. Expires in `expiry` seconds."""
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expiry,
    )


def _read_s3_bytes(key: str) -> tuple[bytes, str]:
    """Fetch an object's bytes and content type from S3."""
    obj = _s3_client().get_object(Bucket=settings.S3_BUCKET, Key=key)
    return obj["Body"].read(), obj.get("ContentType") or "image/jpeg"


def scan_receipt(image_key: str, tab, *, strategy=None) -> dict:
    """
    Run a receipt scanning strategy on the uploaded image and return the parsed
    annotation + date + presigned URL.

    The strategy is chosen from (in order): the `strategy` argument (name or
    instance), then the `scan_strategy` Option (get_or_create'd from the
    registry), falling back to the baseline strategy when that option is
    inactive or holds an unresolvable value.

    Returns {"document_annotation": dict | None, "date": str, "image_url": str,
    "image_key": str, "_scan_metrics": dict}. The `_scan_metrics` key carries
    per-scan analytics properties to be emitted upstream (including `strategy`
    and `scan_total_ms`); api callers should pop it before returning to the
    mobile client.
    """
    from ninjatab.tabs.receipt_scanning.base import ScanContext
    from ninjatab.tabs.receipt_scanning.strategies import (
        STRATEGIES_BY_NAME,
        resolve_strategy,
    )
    from ninjatab.utilities.registry import SCAN_STRATEGY, ensure_option

    if strategy is None:
        strategy = resolve_strategy(ensure_option(SCAN_STRATEGY))
    elif isinstance(strategy, str):
        strategy = STRATEGIES_BY_NAME[strategy]

    tab_id = str(tab.uuid)
    image_bytes, content_type = _read_s3_bytes(image_key)
    ctx = ScanContext(
        image_bytes=image_bytes,
        content_type=content_type,
        default_currency=tab.default_currency,
        tab_id=tab_id,
        s3_base_key=image_key,
    )

    result = strategy.run(ctx)

    logger.info(
        "Receipt scan for tab %s via %s: annotation=%s timings=%s",
        tab_id, strategy.name, result.document_annotation, result.timings,
    )

    return {
        "document_annotation": result.document_annotation,
        "date": result.date,
        "image_url": generate_presigned_url(image_key),
        "image_key": image_key,
        "_scan_metrics": result.metrics,
    }
