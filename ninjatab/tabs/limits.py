from django.conf import settings
from ninja.errors import HttpError

from ninjatab.tabs.models import Bill, BillStatus, Tab


def get_active_bills(tab: Tab):
    return Bill.objects.filter(tab=tab).exclude(status=BillStatus.ARCHIVED)


def check_bill_limit(tab: Tab):
    """Raise 402 if the tab has reached the free bill limit."""
    if get_active_bills(tab).count() >= settings.FREE_TAB_MAX_BILLS:
        raise HttpError(402, "Free tab bill limit reached")


def check_itemised_limit(tab: Tab):
    """Raise 402 if the tab has reached the free itemised bill limit."""
    bills = get_active_bills(tab).prefetch_related('line_items')
    itemised_count = sum(1 for bill in bills if bill.is_itemised)
    if itemised_count >= settings.FREE_TAB_MAX_ITEMISED_BILLS:
        raise HttpError(402, "Free tab itemised bill limit reached")
