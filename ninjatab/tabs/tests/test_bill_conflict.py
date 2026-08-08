import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from ninja.errors import HttpError

from ninjatab.auth.cookies import ACCESS_COOKIE
from ninjatab.auth.jwt_utils import create_access_token
from ninjatab.tabs.api import _check_version
from ninjatab.tabs.models import Bill, Tab
from .factories import TabFactory, TabPersonFactory


@pytest.fixture
def bill(db):
    tab = TabFactory()
    return Bill.objects.create(
        tab=tab,
        description="Dinner",
        currency="GBP",
        creator=TabPersonFactory(tab=tab),
    )


@pytest.mark.django_db
def test_matching_version_passes(bill):
    _check_version(bill, bill.version)


@pytest.mark.django_db
def test_stale_version_is_rejected(bill):
    stale = bill.version
    bill.description = "Dinner (edited elsewhere)"
    bill.save()
    assert bill.version == stale + 1

    with pytest.raises(HttpError) as exc:
        _check_version(bill, stale)
    assert exc.value.status_code == 409


@pytest.mark.django_db
def test_missing_version_skips_the_check(bill):
    """Pre-conflict-detection app builds send nothing and must still write."""
    bill.save()
    _check_version(bill, None)


# The rest go over HTTP: they cover wiring the unit tests can't see — that the
# response carries `version` at all, and that `?version=` binds as a query param.


@pytest.fixture
def api(db):
    user = get_user_model().objects.create_user(
        username="u1", email="u1@example.com", password="x"
    )
    tab = Tab.objects.create(name="T", default_currency="GBP", created_by=user)
    saved = Bill.objects.create(
        tab=tab,
        description="Dinner",
        currency="GBP",
        creator=TabPersonFactory(tab=tab),
    )
    client = Client()
    client.cookies[ACCESS_COOKIE] = create_access_token(user.id, user.email)
    return client, saved


@pytest.mark.django_db
def test_patch_returns_the_new_version(api):
    """The app chains this into submit-splits, so the save can't conflict with
    itself. Dropping `version` from BillSchema would silently break that."""
    client, bill = api
    r = client.patch(
        f"/api/bills/{bill.uuid}",
        data={"description": "A", "version": bill.version},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["version"] == bill.version + 1


@pytest.mark.django_db
def test_version_endpoint_tracks_saves(api):
    """The heartbeat's poll target. Must not collide with GET /bills/{id}."""
    client, bill = api
    r = client.get(f"/api/bills/{bill.uuid}/version")
    assert r.status_code == 200, r.content
    assert r.json() == {"version": bill.version}

    bill.description = "changed elsewhere"
    bill.save()
    assert client.get(f"/api/bills/{bill.uuid}/version").json() == {
        "version": bill.version
    }


@pytest.mark.django_db
def test_version_endpoint_respects_tab_access(api):
    """A stranger must not learn a bill exists by polling it."""
    client, bill = api
    other = get_user_model().objects.create_user(
        username="u2", email="u2@example.com", password="x"
    )
    stranger = Client()
    stranger.cookies[ACCESS_COOKIE] = create_access_token(other.id, other.email)
    assert stranger.get(f"/api/bills/{bill.uuid}/version").status_code == 404


@pytest.mark.django_db
def test_stale_delete_409s_without_deleting(api):
    """`?version=` binds as a query param, and a rejected delete keeps the bill."""
    client, bill = api
    stale = bill.version
    bill.description = "changed elsewhere"
    bill.save()

    r = client.delete(f"/api/bills/{bill.uuid}?version={stale}")
    assert r.status_code == 409, r.content
    # Old app builds render `detail` verbatim in a snackbar, so it must stay a
    # plain string — a list (as a 422 would give) degrades to "(409)".
    assert isinstance(r.json()["detail"], str)
    assert Bill.objects.filter(pk=bill.pk).exists()
