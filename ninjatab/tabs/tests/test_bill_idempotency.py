import pytest
from django.db import IntegrityError

from ninjatab.tabs.models import Bill
from .factories import TabFactory, TabPersonFactory


@pytest.mark.django_db
def test_same_creator_and_client_id_is_rejected():
    """The unique constraint backstops a concurrent duplicate create."""
    tab = TabFactory()
    creator = TabPersonFactory(tab=tab)
    Bill.objects.create(
        tab=tab, description="A", currency="GBP", creator=creator, client_id="abc"
    )
    with pytest.raises(IntegrityError):
        Bill.objects.create(
            tab=tab, description="B", currency="GBP", creator=creator, client_id="abc"
        )


@pytest.mark.django_db
def test_null_client_id_allows_duplicates():
    """Legacy/web bills without a client_id are unaffected by the constraint."""
    tab = TabFactory()
    creator = TabPersonFactory(tab=tab)
    Bill.objects.create(tab=tab, description="A", currency="GBP", creator=creator)
    Bill.objects.create(tab=tab, description="B", currency="GBP", creator=creator)
    assert Bill.objects.filter(creator=creator).count() == 2


@pytest.mark.django_db
def test_same_client_id_across_creators_is_allowed():
    """The key is scoped to the creator, so ids never collide across users."""
    tab = TabFactory()
    c1 = TabPersonFactory(tab=tab)
    c2 = TabPersonFactory(tab=tab)
    Bill.objects.create(
        tab=tab, description="A", currency="GBP", creator=c1, client_id="abc"
    )
    Bill.objects.create(
        tab=tab, description="B", currency="GBP", creator=c2, client_id="abc"
    )
    assert Bill.objects.filter(client_id="abc").count() == 2
