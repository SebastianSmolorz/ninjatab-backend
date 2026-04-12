import factory
from decimal import Decimal
from django.utils import timezone
from ninjatab.tabs.models import Tab, TabPerson, Bill, LineItem, PersonLineItemClaim
from ninjatab.currencies.models import ExchangeRate


class TabFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Tab

    name = factory.Sequence(lambda n: f"Tab {n}")
    default_currency = "GBP"
    settlement_currency = "GBP"


class TabPersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TabPerson

    tab = factory.SubFactory(TabFactory)
    name = factory.Sequence(lambda n: f"Person {n}")


class BillFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Bill

    tab = factory.SubFactory(TabFactory)
    description = factory.Sequence(lambda n: f"Bill {n}")
    currency = "GBP"
    status = "open"
    creator = factory.SubFactory(TabPersonFactory, tab=factory.SelfAttribute("..tab"))


class LineItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LineItem

    bill = factory.SubFactory(BillFactory)
    description = factory.Sequence(lambda n: f"Item {n}")
    value = 0
    split_type = "shares"


class PersonLineItemClaimFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PersonLineItemClaim

    person = factory.SubFactory(TabPersonFactory)
    line_item = factory.SubFactory(LineItemFactory)
    calculated_amount = 0


class ExchangeRateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExchangeRate

    from_currency = "USD"
    to_currency = "GBP"
    rate = Decimal("0.80")
    effective_date = factory.LazyFunction(lambda: timezone.now())
