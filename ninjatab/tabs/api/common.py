"""Helpers shared by the tab and house (group) endpoints."""
import base64
from datetime import datetime

from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery
from ninja.errors import HttpError

from ninjatab.tabs.models import Contact, Settlement, Tab, TabPerson

TABS_PAGE_SIZE = 5
TAB_CURSOR_ORDER = '-created_at,-id'


def _annotate_tab_list(qs):
    """Annotate a Tab queryset with the counts TabListSchema needs."""
    unpaid_settlements = Settlement.objects.filter(tab=OuterRef('pk'), paid=False)
    people_count_subquery = Subquery(
        TabPerson.objects.filter(tab=OuterRef('pk')).values('tab').annotate(c=Count('id')).values('c'),
        output_field=IntegerField(),
    )
    return qs.annotate(
        bill_count=Count('bills', distinct=True),
        people_count=people_count_subquery,
        all_settlements_paid=~Exists(unpaid_settlements),
        paid_settlements_count=Count('settlements', filter=Q(settlements__paid=True), distinct=True),
        total_settlements_count=Count('settlements', distinct=True),
    )



def _apply_tab_cursor(qs, cursor: str | None):
    """Cursor pagination for tabs, ordered by (-created_at, -id)."""
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor).decode()
            order, ts_str, obj_id = decoded.split('|', 2)
            if order != TAB_CURSOR_ORDER:
                raise ValueError("cursor order mismatch")
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = int(obj_id)
            qs = qs.filter(
                Q(created_at__lt=cursor_ts) |
                Q(created_at=cursor_ts, id__lt=cursor_id)
            )
        except (ValueError, TypeError):
            raise HttpError(400, "Invalid cursor")

    qs = qs.order_by('-created_at', '-id')
    items = list(qs[:TABS_PAGE_SIZE + 1])

    next_cursor = None
    if len(items) > TABS_PAGE_SIZE:
        items = items[:TABS_PAGE_SIZE]
        last = items[-1]
        raw = f"{TAB_CURSOR_ORDER}|{last.created_at.isoformat()}|{last.id}"
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

    return items, next_cursor



def _sync_contacts_for_tab(tab):
    """Create bidirectional Contact records for every user pair on a tab."""
    user_ids = list(
        TabPerson.objects.filter(tab=tab, user__isnull=False)
        .values_list('user_id', flat=True)
    )
    if len(user_ids) < 2:
        return
    for i, uid_a in enumerate(user_ids):
        for uid_b in user_ids[i + 1:]:
            Contact.objects.get_or_create(owner_id=uid_a, contact_user_id=uid_b)
            Contact.objects.get_or_create(owner_id=uid_b, contact_user_id=uid_a)



def _create_period_tab(group, base_name, period_index=1):
    """Create a fresh period Tab for a house and project the roster into it.

    Copies each TabGroupMember to a TabPerson (name + user link, traced via the
    `member` FK). The new tab starts empty of bills.
    """
    tab = Tab.objects.create(
        name=base_name,
        default_currency=group.default_currency,
        settlement_currency=group.settlement_currency,
        created_by=group.created_by,
        group=group,
        period_index=period_index,
    )
    for member in group.members.all():
        TabPerson.objects.create(
            tab=tab,
            name=member.name,
            user=member.user,
            member=member,
        )
    _sync_contacts_for_tab(tab)
    return tab


