import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from ninjatab.tabs.models import LineItem, PersonLineItemClaim
from .factories import BillFactory, TabFactory, TabPersonFactory

User = get_user_model()


@pytest.fixture
def tab(db):
    tab = TabFactory(name="Jans", is_pro=True)
    user = User.objects.create_user(username="seb", email="seb@tab.ninja")
    seb = TabPersonFactory(tab=tab, name="Seb", user=user)
    mel = TabPersonFactory(tab=tab, name="Mel")
    bill = BillFactory(tab=tab, description="Tesco", creator=seb, paid_by=seb)
    item = LineItem.objects.create(bill=bill, description="Ice Tea", value=220)
    PersonLineItemClaim.objects.create(
        person=seb, line_item=item, split_value=1, calculated_amount=73, settlement_amount=73
    )
    PersonLineItemClaim.objects.create(
        person=mel, line_item=item, split_value=2, calculated_amount=147, settlement_amount=147
    )
    return tab


def publish(tab):
    tab.is_public = True
    tab.save()
    return tab


def get_public(tab):
    return Client().get(f"/api/tabs/public/{tab.public_slug or tab.uuid}")


@pytest.mark.django_db
def test_private_tab_is_not_readable(tab):
    assert get_public(tab).status_code == 404


@pytest.mark.django_db
def test_archived_public_tab_is_still_readable(tab):
    """Archiving is the app's soft-delete; it must not break a live public link.

    See the TEMPORARY note on retrieve_public_tab.
    """
    tab.is_archived = True
    publish(tab)
    assert get_public(tab).status_code == 200


@pytest.mark.django_db
def test_slug_is_generated_on_publish(tab):
    assert tab.public_slug is None
    publish(tab)
    assert tab.public_slug == "jans"


@pytest.mark.django_db
def test_slug_survives_a_rename(tab):
    publish(tab)
    tab.name = "Jans Holiday"
    tab.save()
    assert tab.public_slug == "jans"


@pytest.mark.django_db
def test_colliding_names_get_distinct_slugs(tab):
    publish(tab)
    other = publish(TabFactory(name="Jans"))
    assert other.public_slug == "jans-2"


@pytest.mark.django_db
def test_uuid_is_not_a_valid_public_key(tab):
    publish(tab)
    assert Client().get(f"/api/tabs/public/{tab.uuid}").status_code == 404


@pytest.mark.django_db
def test_public_tab_payload(tab):
    publish(tab)

    response = get_public(tab)
    assert response.status_code == 200
    data = response.json()

    assert data["name"] == "Jans"
    assert data["group_spend"] == 220
    assert [p["name"] for p in data["people"]] == ["Seb", "Mel"]
    assert [p["spend"] for p in data["people"]] == [73, 147]

    bill = data["bills"][0]
    assert bill["total_amount"] == 220
    assert bill["paid_by"] == "Seb"
    assert bill["line_items"][0]["claims"][1] == {
        "person_id": str(tab.people.get(name="Mel").uuid),
        "person_name": "Mel",
        "split_value": 2,
        "amount": 147,
    }

    # No PII beyond names, and no internal ids.
    assert "seb@tab.ninja" not in response.content.decode()
    assert "invite_code" not in data
    assert set(data["people"][0]) == {"id", "name", "spend"}
