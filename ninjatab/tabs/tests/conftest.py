import pytest
from .factories import TabFactory, TabPersonFactory


@pytest.fixture(autouse=True)
def clear_exchange_cache():
    from ninjatab.currencies.exchange import clear_rate_cache
    clear_rate_cache()
    yield
    clear_rate_cache()


@pytest.fixture
def tab_with_people(db):
    """Returns a factory function that creates a tab with named people.

    Usage:
        tab, people = tab_with_people(["Alice", "Bob"])
        alice = people["Alice"]
    """
    def _make(names, currency="GBP"):
        tab = TabFactory(default_currency=currency, settlement_currency=currency)
        people = {name: TabPersonFactory(tab=tab, name=name) for name in names}
        return tab, people
    return _make
