import json
import logging
import threading
import uuid
from datetime import timedelta

import boto3
import sentry_sdk
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from ninjatab.tabs.models import ReceiptScan, ReceiptScanStatus, Tab
from ninjatab.tabs.receipt_scanning.ledger import flatten_for_v1
from ninjatab.tabs.scan_analytics import fire_scan_exception, fire_scan_result

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


def s3_client():
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

    s3_client().upload_fileobj(
        file,
        settings.S3_BUCKET,
        key,
        ExtraArgs={"ACL": "private", "ContentType": file.content_type},
    )
    return key


def generate_presigned_url(key: str, expiry: int = 3600) -> str:
    """Generate a pre-signed URL for a private S3 object. Expires in `expiry` seconds."""
    return s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expiry,
    )


def _read_s3_bytes(key: str) -> tuple[bytes, str]:
    """Fetch an object's bytes and content type from S3."""
    obj = s3_client().get_object(Bucket=settings.S3_BUCKET, Key=key)
    return obj["Body"].read(), obj.get("ContentType") or "image/jpeg"


def scan_receipt(image_key: str, tab, *, strategy=None) -> tuple[dict, dict]:
    """
    Run a receipt scanning strategy on the uploaded image and return the parsed
    annotation + date + presigned URL.

    The strategy is chosen from (in order): the `strategy` argument (name or
    instance), then the `scan_strategy` Option (get_or_create'd from the
    registry), falling back to the baseline strategy when that option is
    inactive or holds an unresolvable value.

    Returns (result, metrics). `result` is {"document_annotation": dict | None,
    "date": str, "image_url": str, "image_key": str}; `metrics` holds the
    per-scan analytics properties (including `strategy` and `scan_total_ms`).

    The annotation is the reconciled ledger (`items` plus typed `adjustments`).
    The v1 endpoint and background scans run `ledger.flatten_for_v1` over it;
    v2 returns it as-is.
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
    }, result.metrics


def finish_scan(user_uuid, tab, image_key: str) -> dict:
    """Run OCR on a stored image, fire the analytics, and return the scan
    result (the reconciled ledger annotation, as `/upload-receipt-v2` returns
    it)."""
    try:
        result, metrics = scan_receipt(image_key, tab)
    except Exception as e:
        fire_scan_exception(user_uuid, tab, e)
        raise
    increment_scan_count(tab)
    fire_scan_result(user_uuid, tab, result, metrics)
    result["scan_session_id"] = image_key
    return result


# Background scans: the phone uploads, then polls a ReceiptScan row by its
# client_id while the OCR runs here.

# A pending scan untouched for this long has lost its thread (worker restart).
STALE_SCAN_AFTER = timedelta(minutes=3)
MAX_SCAN_ATTEMPTS = 3


class ScanAttemptsExhausted(Exception):
    pass


def create_background_scan(tab, user, client_id: str, image_key: str) -> ReceiptScan:
    """Record an uploaded receipt and start scanning it."""
    try:
        scan = ReceiptScan.objects.create(
            tab=tab, created_by=user, client_id=client_id,
            image_key=image_key, attempts=1,
        )
    except IntegrityError:
        # A concurrent retry with the same client_id won the race.
        return ReceiptScan.objects.get(created_by=user, client_id=client_id)
    _spawn_scan(scan.pk)
    return scan


def resume_scan(scan: ReceiptScan) -> None:
    """Re-run a scan that failed or lost its thread, updating `scan` in place.
    Raises ScanAttemptsExhausted past MAX_SCAN_ATTEMPTS runs: a receipt that
    always fails must not re-run the paid OCR on every retry."""
    stale = (scan.status == ReceiptScanStatus.PENDING
             and timezone.now() - scan.updated_at > STALE_SCAN_AFTER)
    if scan.status == ReceiptScanStatus.FAILED or stale:
        if scan.attempts >= MAX_SCAN_ATTEMPTS:
            raise ScanAttemptsExhausted()
        _claim_scan(scan)


def _claim_scan(scan: ReceiptScan) -> None:
    """(Re)start a scan. The update is conditional on the row's current
    `updated_at`, so concurrent polls start it only once. Refreshes `scan`
    either way, so a lost race reports the winner's state."""
    claimed = ReceiptScan.objects.filter(
        pk=scan.pk, updated_at=scan.updated_at,
    ).update(status=ReceiptScanStatus.PENDING, error="",
             attempts=F("attempts") + 1, updated_at=timezone.now())
    scan.refresh_from_db()
    if claimed:
        _spawn_scan(scan.pk)


def _spawn_scan(scan_id: int) -> None:
    """Run the scan off the request thread, once the row it reads is committed
    (immediately under autocommit; after the block inside `transaction.atomic`).

    ponytail: an in-process daemon thread, lost if gunicorn restarts mid-scan;
    `resume_scan` re-runs stale ones on the next poll. Move to a real queue if
    deploys or scan volume start losing scans.
    """
    def target():
        try:
            _run_scan(scan_id)
        finally:
            connection.close()

    transaction.on_commit(threading.Thread(target=target, daemon=True).start)


def _run_scan(scan_id: int) -> None:
    """Scan a stored ReceiptScan and write the v1-shaped result onto its row."""
    # Any other error here escapes the thread: Sentry's default threading
    # integration reports it, and the stale-pending reclaim re-runs the scan.
    scan = ReceiptScan.objects.select_related("tab", "created_by").filter(pk=scan_id).first()
    if scan is None:
        return  # deleted with its tab while queued
    try:
        result = finish_scan(scan.created_by.uuid, scan.tab, scan.image_key)
        result["document_annotation"] = flatten_for_v1(result["document_annotation"])
    except Exception as e:
        logger.exception("Receipt scan %s failed", scan.client_id)
        sentry_sdk.capture_exception(e)
        scan.status = ReceiptScanStatus.FAILED
        scan.error = f"{type(e).__name__}: {e}"
        scan.save(update_fields=["status", "error", "updated_at"])
        return
    scan.status = ReceiptScanStatus.DONE
    # Round-trip through JSON so non-JSON values (dates, Decimals) are stored
    # as strings rather than failing the JSONField save.
    scan.result = json.loads(json.dumps(result, default=str))
    scan.error = ""
    scan.save(update_fields=["status", "result", "error", "updated_at"])
