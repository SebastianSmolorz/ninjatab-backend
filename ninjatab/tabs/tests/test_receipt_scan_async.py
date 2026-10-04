from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from ninja.errors import HttpError

from ninjatab.tabs import receipt_service, scan_analytics
from ninjatab.tabs.api import receipts as api
from ninjatab.tabs.models import ReceiptScan
from ninjatab.tabs.receipt_service import MAX_SCAN_ATTEMPTS, MAX_SCANS_PER_TAB
from .factories import TabFactory


@pytest.fixture
def env(db, monkeypatch):
    """A user owning a tab, with storage/OCR mocked and the background thread
    run inline so the test sees its writes."""
    calls = SimpleNamespace(scans=0, uploads=0)

    def fake_upload(file, tab_id):
        calls.uploads += 1
        return f"receipts/{tab_id}/img.jpg"

    def fake_scan(image_key, tab, **_):
        calls.scans += 1
        return ({"document_annotation": {"items": []}, "date": "2026-09-24",
                 "image_key": image_key}, {},
                [{"call": 0, "annotation": {"items": []}, "markdown": "TOTAL 1.00"}])

    monkeypatch.setattr(api, "upload_to_spaces", fake_upload)
    monkeypatch.setattr(receipt_service, "scan_receipt", fake_scan)
    monkeypatch.setattr(receipt_service, "flatten_for_v1", lambda a: {**a, "flat": True})
    monkeypatch.setattr(api, "safe_capture", lambda *a, **k: None)
    monkeypatch.setattr(scan_analytics, "safe_capture", lambda *a, **k: None)
    monkeypatch.setattr(receipt_service, "_spawn_scan", receipt_service._run_scan)

    user = get_user_model().objects.create_user(username="u", email="u@example.com")
    tab = TabFactory(created_by=user)
    request = SimpleNamespace(auth=user)
    return SimpleNamespace(calls=calls, user=user, tab=tab, request=request)


def _file():
    return SimpleUploadedFile("r.jpg", b"jpeg", content_type="image/jpeg")


def _post(env, client_id="c1"):
    return api.create_receipt_scan(env.request, str(env.tab.uuid), client_id=client_id, file=_file())


def test_upload_scans_and_poll_returns_v1_result(env):
    _post(env)
    state = api.retrieve_receipt_scan(env.request, str(env.tab.uuid), "c1")
    assert state["status"] == "done"
    assert state["result"]["document_annotation"] == {"items": [], "flat": True}
    assert state["result"]["scan_session_id"] == state["result"]["image_key"]
    env.tab.refresh_from_db()
    assert env.tab.receipt_scan_count == 1


def test_readings_are_stored_but_never_returned(env):
    _post(env)
    scan = ReceiptScan.objects.get(client_id="c1")
    assert scan.readings == [{"call": 0, "annotation": {"items": []}, "markdown": "TOTAL 1.00"}]
    state = api.retrieve_receipt_scan(env.request, str(env.tab.uuid), "c1")
    assert "readings" not in state["result"]


def test_retry_with_same_client_id_does_not_rescan(env):
    _post(env)
    _post(env)
    assert (env.calls.uploads, env.calls.scans) == (1, 1)
    assert ReceiptScan.objects.count() == 1


def test_failed_scan_is_rerun_on_retry(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("mistral down")
    real = receipt_service.finish_scan
    monkeypatch.setattr(receipt_service, "finish_scan", boom)
    assert _post(env)["status"] == "pending"
    scan = ReceiptScan.objects.get()
    assert scan.status == "failed" and "mistral down" in scan.error

    monkeypatch.setattr(receipt_service, "finish_scan", real)
    _post(env)
    scan.refresh_from_db()
    assert scan.status == "done"
    assert env.calls.uploads == 1  # re-ran on the stored image


def test_stale_pending_scan_is_restarted_on_poll(env, monkeypatch):
    monkeypatch.setattr(receipt_service, "_spawn_scan", lambda scan_id: None)  # thread "dies"
    _post(env)
    scan = ReceiptScan.objects.get()
    assert api.retrieve_receipt_scan(env.request, str(env.tab.uuid), "c1")["status"] == "pending"

    ReceiptScan.objects.filter(pk=scan.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
    monkeypatch.setattr(receipt_service, "_spawn_scan", receipt_service._run_scan)
    api.retrieve_receipt_scan(env.request, str(env.tab.uuid), "c1")
    scan.refresh_from_db()
    assert scan.status == "done"


def test_scan_limit_returns_409(env):
    env.tab.receipt_scan_count = MAX_SCANS_PER_TAB
    env.tab.save()
    with pytest.raises(HttpError) as e:
        _post(env)
    assert e.value.status_code == 409


def test_poll_reruns_failed_scan_until_attempts_cap(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("mistral down")
    monkeypatch.setattr(receipt_service, "finish_scan", boom)
    _post(env)  # attempt 1
    for _ in range(MAX_SCAN_ATTEMPTS - 1):  # attempts 2..3, via the poll
        api.retrieve_receipt_scan(env.request, str(env.tab.uuid), "c1")
    scan = ReceiptScan.objects.get()
    assert (scan.status, scan.attempts) == ("failed", MAX_SCAN_ATTEMPTS)

    for call in (lambda: api.retrieve_receipt_scan(env.request, str(env.tab.uuid), "c1"),
                 lambda: _post(env)):
        with pytest.raises(HttpError) as e:
            call()
        assert e.value.status_code == 422
    assert env.calls.uploads == 1


def test_scan_outcome_is_not_shadowed_by_tab_id_route(client):
    # Unauthenticated: 401 from the right route, not 405 from `/{tab_id}`.
    response = client.post("/api/tabs/scan-outcome", data={}, content_type="application/json")
    assert response.status_code == 401
