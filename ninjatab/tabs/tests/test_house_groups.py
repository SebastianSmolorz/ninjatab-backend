"""Tests for ongoing "house" tabs (TabGroup) and the settle-period roll."""
import json

import pytest
from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model
from django.test import Client

from ninjatab.tabs.models import (
    Tab, TabPerson, TabGroup, TabGroupMember, Settlement,
)
from ninjatab.auth.jwt_utils import create_access_token
from .factories import TabFactory, BillFactory, LineItemFactory

User = get_user_model()


def _auth(user):
    token = create_access_token(user.id, user.email)
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _user(email):
    return User.objects.create(username=email, email=email)


def _post(client, url, body, user):
    return client.post(
        url, data=json.dumps(body), content_type="application/json", **_auth(user)
    )


def _make_group(creator, member_names, currency="GBP"):
    group = TabGroup.objects.create(
        name="Flat 3B", created_by=creator,
        default_currency=currency, settlement_currency=currency,
    )
    for n in member_names:
        TabGroupMember.objects.create(group=group, name=n)
    return group


def _open_period(group, period_index=1):
    """Create a period tab projecting the group roster (mirrors _create_period_tab)."""
    tab = TabFactory(
        group=group, period_index=period_index,
        created_by=group.created_by,
        default_currency=group.default_currency,
        settlement_currency=group.settlement_currency,
    )
    for m in group.members.all():
        TabPerson.objects.create(tab=tab, name=m.name, user=m.user, member=m)
    return tab


def _add_bill(tab, payer, value=1000):
    bill = BillFactory(tab=tab, creator=payer, paid_by=payer, currency=tab.default_currency)
    LineItemFactory(bill=bill, value=value)
    return bill


# ---------------------------------------------------------------------------
# Model-level invariants
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_active_period_per_group_constraint():
    """A house may have at most one open, non-archived period at a time."""
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice"])
    _open_period(group)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _open_period(group, period_index=2)


@pytest.mark.django_db
def test_settled_and_archived_periods_dont_count_toward_constraint():
    """Closing the prior period frees the slot for a new open one."""
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice"])
    p1 = _open_period(group)
    p1.is_settled = True
    p1.save(update_fields=["is_settled"])
    # No IntegrityError: p1 is settled, so a second open period is allowed.
    _open_period(group, period_index=2)
    assert group.tabs.filter(is_settled=False).count() == 1


@pytest.mark.django_db
def test_standalone_tabs_unaffected_by_constraint():
    """Tabs without a group can coexist freely (NULL group is exempt)."""
    TabFactory()
    TabFactory()
    assert Tab.objects.filter(group__isnull=True).count() == 2


# ---------------------------------------------------------------------------
# settle-period endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_settle_period_rolls_roster_and_freezes_old():
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice", "Bob"])
    period = _open_period(group)
    alice = period.people.get(name="Alice")
    _add_bill(period, alice, value=2000)

    client = Client()
    resp = _post(client, f"/api/tabs/{period.uuid}/settle-period", {}, creator)
    assert resp.status_code == 200, resp.content
    body = resp.json()

    # Old period is frozen with a snapshot total.
    period.refresh_from_db()
    assert period.is_settled is True
    assert period.settlement_currency_settled_total == 2000

    # Response is a NEW, empty active period with the same roster.
    assert body["id"] != str(period.uuid)
    assert body["is_settled"] is False
    assert body["bill_count"] == 0
    assert body["period_index"] == 2
    assert body["group_id"] == str(group.uuid)
    assert {p["name"] for p in body["people"]} == {"Alice", "Bob"}

    # New TabPerson rows are distinct from the old ones but trace the same members.
    new_tab = Tab.objects.get(uuid=body["id"])
    assert new_tab.people.exclude(member__isnull=True).count() == 2
    assert set(new_tab.people.values_list("member__name", flat=True)) == {"Alice", "Bob"}


@pytest.mark.django_db
def test_settle_period_periods_are_independent():
    """The new period starts at zero; the old period keeps its unpaid settlements."""
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice", "Bob"])
    period = _open_period(group)
    alice = period.people.get(name="Alice")
    bob = period.people.get(name="Bob")
    _add_bill(period, alice, value=2000)
    Settlement.objects.create(
        tab=period, from_person=bob, to_person=alice, amount=1000, currency="GBP",
    )

    client = Client()
    resp = _post(client, f"/api/tabs/{period.uuid}/settle-period", {}, creator)
    new_tab = Tab.objects.get(uuid=resp.json()["id"])

    # Old unpaid settlement is preserved on the closed period.
    assert period.settlements.filter(paid=False).count() == 1
    # New period carries no settlements or bills.
    assert new_tab.settlements.count() == 0
    assert new_tab.bills.count() == 0


@pytest.mark.django_db
def test_settle_period_rejects_non_group_tab():
    creator = _user("a@x.com")
    tab = TabFactory(created_by=creator)
    person = TabPerson.objects.create(tab=tab, name="Alice")
    _add_bill(tab, person)
    client = Client()
    resp = _post(client, f"/api/tabs/{tab.uuid}/settle-period", {}, creator)
    assert resp.status_code == 400
    assert "not part of a house" in resp.json()["detail"]


@pytest.mark.django_db
def test_settle_period_rejects_already_settled():
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice"])
    period = _open_period(group)
    alice = period.people.get(name="Alice")
    _add_bill(period, alice)
    period.is_settled = True
    period.save(update_fields=["is_settled"])
    client = Client()
    resp = _post(client, f"/api/tabs/{period.uuid}/settle-period", {}, creator)
    assert resp.status_code == 400
    assert "already settled" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Group endpoints + access control
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_create_group_opens_first_period():
    creator = _user("a@x.com")
    client = Client()
    resp = _post(client, "/api/groups/", {
        "name": "Flat 3B",
        "settlement_currency": "GBP",
        "members": [{"name": "Alice"}, {"name": "Bob"}],
    }, creator)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert {m["name"] for m in body["members"]} == {"Alice", "Bob"}
    assert body["current_period"] is not None
    assert body["current_period"]["period_index"] == 1

    group = TabGroup.objects.get(uuid=body["id"])
    assert group.tabs.count() == 1
    assert group.current_period.people.count() == 2


@pytest.mark.django_db
def test_group_detail_aggregates_spend_across_periods():
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice"])
    p1 = _open_period(group)
    alice1 = p1.people.get(name="Alice")
    _add_bill(p1, alice1, value=3000)

    client = Client()
    # Roll: p1 closes with a 3000 snapshot, p2 opens.
    resp = _post(client, f"/api/tabs/{p1.uuid}/settle-period", {}, creator)
    p2 = Tab.objects.get(uuid=resp.json()["id"])
    _add_bill(p2, p2.people.get(name="Alice"), value=500)

    detail = client.get(f"/api/groups/{group.uuid}", **_auth(creator)).json()
    # 3000 (settled snapshot) + 500 (live open period) = 3500
    assert detail["group_total_spend"] == 3500
    assert len(detail["periods"]) == 2


@pytest.mark.django_db
def test_member_linked_user_can_access_group_and_periods():
    creator = _user("creator@x.com")
    member_user = _user("bob@x.com")
    group = _make_group(creator, ["Alice", "Bob"])
    # Link Bob's member to a real user, then project the roster.
    group.members.filter(name="Bob").update(user=member_user)
    period = _open_period(group)

    client = Client()
    # Bob is not the creator, but is a member → can read the group...
    g = client.get(f"/api/groups/{group.uuid}", **_auth(member_user))
    assert g.status_code == 200
    # ...and the period tab.
    t = client.get(f"/api/tabs/{period.uuid}", **_auth(member_user))
    assert t.status_code == 200

    # A stranger cannot.
    stranger = _user("nope@x.com")
    assert client.get(f"/api/groups/{group.uuid}", **_auth(stranger)).status_code == 404


@pytest.mark.django_db
def test_add_member_projects_into_current_period():
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice"])
    period = _open_period(group)

    client = Client()
    resp = _post(client, f"/api/groups/{group.uuid}/members", {"name": "Carol"}, creator)
    assert resp.status_code == 200
    assert {m["name"] for m in resp.json()["members"]} == {"Alice", "Carol"}
    assert period.people.filter(name="Carol").exists()


@pytest.mark.django_db
def test_list_tabs_excludes_house_periods():
    """The individual-tabs list never shows house period tabs."""
    creator = _user("a@x.com")
    standalone = TabFactory(created_by=creator)
    TabPerson.objects.create(tab=standalone, name="Solo", user=creator)
    group = _make_group(creator, ["Alice"])
    group.members.filter(name="Alice").update(user=creator)
    _open_period(group)

    client = Client()
    items = client.get("/api/tabs/", **_auth(creator)).json()["items"]
    assert {t["id"] for t in items} == {str(standalone.uuid)}


@pytest.mark.django_db
def test_list_group_periods_returns_all_periods_newest_first():
    creator = _user("a@x.com")
    group = _make_group(creator, ["Alice"])
    p1 = _open_period(group)
    _add_bill(p1, p1.people.get(name="Alice"), value=1000)

    client = Client()
    # Roll once so the house has a settled period + a current one.
    resp = _post(client, f"/api/tabs/{p1.uuid}/settle-period", {}, creator)
    p2_id = resp.json()["id"]

    items = client.get(f"/api/groups/{group.uuid}/periods", **_auth(creator)).json()["items"]
    assert [t["id"] for t in items] == [p2_id, str(p1.uuid)]  # newest first
    assert [t["period_index"] for t in items] == [2, 1]
    # The settled period is flagged as such for the history UI.
    assert next(t for t in items if t["id"] == str(p1.uuid))["is_settled"] is True
