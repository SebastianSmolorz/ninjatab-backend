"""Receipt upload + scan endpoints, mounted under `/tabs` alongside tab_router.

A router of its own so it can be mounted *before* tab_router: `/scan-outcome`
would otherwise be shadowed by tab_router's `/{tab_id}` and answer 405.
"""
import json
import logging

from django.conf import settings
from django.shortcuts import get_object_or_404
from ninja import File, Form, Router, UploadedFile
from ninja.errors import HttpError

from ninjatab.auth.bearer import JWTBearer
from ninjatab.tabs.models import ReceiptScan, Tab
from ninjatab.tabs.receipt_scanning.ledger import flatten_for_v1
from ninjatab.tabs.receipt_service import (
    ScanAttemptsExhausted, ScanLimitExceeded, check_scan_limit,
    create_background_scan, finish_scan, resume_scan, upload_to_spaces,
    validate_upload,
)
from ninjatab.tabs.scan_analytics import fire_scan_outcome
from ninjatab.tabs.schemas import ScanOutcomeSchema
from ninjatab.utilities.analytics import safe_capture

logger = logging.getLogger("app")

receipt_router = Router(tags=["receipts"], auth=JWTBearer())


@receipt_router.post("/{tab_id}/upload-receipt")
def upload_receipt(request, tab_id: str, file: UploadedFile = File(...)):
    """Upload a receipt image, run OCR, and return the parsed annotation as one
    flat `items` list — the contract the shipped mobile client reads.

    Charges reach it as extra rows carrying a `category`, item discounts as
    negative rows beneath a grossed-up item, and at most one row is a tax. See
    `/upload-receipt-v2` for the same scan without that flattening.
    """
    result = _scan_upload(request, tab_id, file)
    result["document_annotation"] = flatten_for_v1(result["document_annotation"])
    return _debug_dump("upload-receipt", result)


@receipt_router.post("/{tab_id}/upload-receipt-v2")
def upload_receipt_v2(request, tab_id: str, file: UploadedFile = File(...)):
    """The same scan as `/upload-receipt`, returning the reconciled ledger:
    `items` holds only what someone ordered, and `adjustments` holds every
    receipt-level charge, each with a `type` (tax | tip | service | fee |
    discount) and the `split` mode it defaults to (proportional | even).

    `items_total + adjustments_total == grand_total`, which is what
    `receipt_total` is checked against.
    """
    return _debug_dump("upload-receipt-v2", _scan_upload(request, tab_id, file))


def _debug_dump(label: str, result: dict) -> dict:
    """In dev, print a scan response to the console. Returns it unchanged.

    print, not the "app" logger: that one only writes to app.log, and the point
    is to read the scan next to the request line in the runserver output.
    """
    if settings.DEBUG:
        print(f"--- {label} ---")
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return result


def _start_scan(request, tab_id: str, file):
    """The fast half of a scan: access + limit checks, then store the image.
    Returns (tab, image_key)."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)

    try:
        check_scan_limit(tab)
    except ScanLimitExceeded as e:
        safe_capture(request.auth.uuid, "scan_limit_hit", properties={"tab_id": str(tab.uuid)})
        # 409, not 429: this is a permanent per-tab sanity backstop, not a
        # rate-limit. The client must stop retrying rather than back off.
        # (401/403 are avoided — they trigger client logout.)
        raise HttpError(409, str(e))

    try:
        validate_upload(file)
    except ValueError as e:
        raise HttpError(400, str(e))

    return tab, upload_to_spaces(file, tab_id)


def _scan_upload(request, tab_id: str, file) -> dict:
    """Upload a receipt and scan it within the request. Shared by both
    synchronous endpoint versions — they differ only in how the annotation is
    presented."""
    tab, image_key = _start_scan(request, tab_id, file)
    return finish_scan(request.auth.uuid, tab, image_key)


def _scan_state(scan) -> dict:
    # `error` stays server-side (admin + Sentry): it's raw provider/S3 detail.
    return {"status": scan.status, "result": scan.result}


def _resumed_state(scan) -> dict:
    """Re-run the scan if it failed or lost its thread, then return its state.
    Out of attempts answers 422, which the app treats as permanent."""
    try:
        resume_scan(scan)
    except ScanAttemptsExhausted:
        raise HttpError(422, "This receipt couldn't be scanned")
    return _scan_state(scan)


@receipt_router.post("/{tab_id}/receipt-scans")
def create_receipt_scan(request, tab_id: str, client_id: str = Form(..., max_length=64),
                        file: UploadedFile = File(...)):
    """Upload a receipt and scan it in the background; poll
    `GET /{tab_id}/receipt-scans/{client_id}` for the result.

    Idempotent on `client_id` (the app's offline-queue localId): a retry
    returns the existing scan instead of uploading and scanning again, and
    re-runs it if it had failed.
    """
    existing = ReceiptScan.objects.filter(
        created_by=request.auth, client_id=client_id, tab__uuid=tab_id,
    ).first()
    if existing:
        return _resumed_state(existing)

    tab, image_key = _start_scan(request, tab_id, file)
    return _scan_state(create_background_scan(tab, request.auth, client_id, image_key))


@receipt_router.get("/{tab_id}/receipt-scans/{client_id}")
def retrieve_receipt_scan(request, tab_id: str, client_id: str):
    """The state of a background receipt scan. `result` matches the
    `/upload-receipt` response once `status` is `done`. Re-runs a failed scan,
    so the app's retry can poll here instead of re-uploading the image."""
    scan = get_object_or_404(
        ReceiptScan, created_by=request.auth, client_id=client_id, tab__uuid=tab_id,
    )
    return _resumed_state(scan)


@receipt_router.post("/scan-outcome")
def scan_outcome(request, payload: ScanOutcomeSchema):
    """Record a non-submit terminal outcome for a receipt scan.

    Always returns 200; failures are logged but never surfaced to the client.
    """
    if payload.outcome not in {"rescanned", "abandoned"}:
        logger.warning("scan_outcome got invalid outcome=%s", payload.outcome)
        return {"ok": True}

    fire_scan_outcome(request.auth.uuid, payload.scan_session_id, payload.outcome)
    return {"ok": True}
