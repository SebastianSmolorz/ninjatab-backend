import base64
import uuid
import logging
import sentry_sdk

from datetime import datetime


from ninja import Router, Schema, UploadedFile, File
from ninja.errors import HttpError
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, Exists, OuterRef, Subquery, Sum, IntegerField
from django.db.models.functions import Coalesce
from django.contrib.auth import get_user_model
from django.utils import timezone

from ninjatab.tabs.models import *
from ninjatab.tabs.schemas import *
from ninjatab.currencies.currency_utils import minor_to_decimal
from ninjatab.tabs.simp import simp_tab
from ninjatab.currencies.exchange import convert_amount, ExchangeRateNotFoundError, clear_rate_cache
from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.schemas import MagicLinkSuccessSchema
from ninjatab.tabs.limits import check_bill_limit, check_itemised_limit
from ninjatab.tabs.demo import create_demo_tab as _create_demo_tab
from ninjatab.utilities.analytics import safe_capture


User = get_user_model()

tab_router = Router(tags=["tabs"], auth=JWTBearer())
bill_router = Router(tags=["bills"], auth=JWTBearer())
group_router = Router(tags=["groups"], auth=JWTBearer())

logger = logging.getLogger("app")

PAGE_SIZE = 15
TABS_PAGE_SIZE = 5

TAB_CURSOR_ORDER = '-created_at,-id'
BILL_CURSOR_ORDER = '-date,-id'


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


def _apply_bill_cursor(qs, cursor: str | None):
    """Cursor pagination for bills, ordered by (-date, -id)."""
    from datetime import date as date_type
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor).decode()
            order, date_str, obj_id = decoded.split('|', 2)
            if order != BILL_CURSOR_ORDER:
                raise ValueError("cursor order mismatch")
            cursor_date = date_type.fromisoformat(date_str)
            cursor_id = int(obj_id)
            qs = qs.filter(
                Q(date__lt=cursor_date) |
                Q(date=cursor_date, id__lt=cursor_id)
            )
        except (ValueError, TypeError):
            raise HttpError(400, "Invalid cursor")

    qs = qs.order_by('-date', '-id')
    items = list(qs[:PAGE_SIZE + 1])

    next_cursor = None
    if len(items) > PAGE_SIZE:
        items = items[:PAGE_SIZE]
        last = items[-1]
        raw = f"{BILL_CURSOR_ORDER}|{last.date.isoformat()}|{last.id}"
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


def _close_tab(tab, actor):
    """Settle a tab in place: snapshot total spend, freeze it, emit analytics.

    `tab` must already be loaded. Raises HttpError(400) if it has no settleable
    bills. Shared by the `/close` endpoint and the house period-roll flow.
    """
    bills = list(tab.bills.prefetch_related('line_items').exclude(status=BillStatus.ARCHIVED.value))
    if not bills:
        raise HttpError(400, "Cannot settle a tab with no bills")

    # Snapshot total spent in settlement currency (minor units)
    total = 0
    for bill in bills:
        bill_total = sum((li.value or 0) for li in bill.line_items.all())
        if bill.currency != tab.settlement_currency:
            bill_total = convert_amount(bill_total, bill.currency, tab.settlement_currency)
        total += bill_total

    tab.is_settled = True
    tab.settled_at = timezone.now()
    tab.settlement_currency_settled_total = total
    tab.save()

    safe_capture(getattr(actor, "uuid", None), "tab_settled", properties={
        "tab_id": str(tab.uuid),
        "bill_count": len(bills),
        "settlement_currency": tab.settlement_currency,
        "total_minor_units": total,
    })
    return total


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


def _serialize_tab(tab_id):
    """Re-fetch a tab by pk with the standard prefetch set used for TabSchema."""
    return Tab.objects.select_related('group').prefetch_related(
        'people__user',
        'bills__line_items',
        'settlements__from_person__user',
        'settlements__to_person__user__payment_methods',
    ).get(id=tab_id)


@tab_router.post("/", response=TabSchema)
@transaction.atomic
def create_tab(request, payload: TabCreateSchema):
    """Create a new tab with people"""
    tab = Tab.objects.create(
        name=payload.name,
        description=payload.description,
        default_currency=payload.default_currency,
        settlement_currency=payload.settlement_currency,
        created_by=request.auth
    )

    for person_data in payload.people:
        user = None
        if person_data.user_id:
            user = get_object_or_404(User, uuid=person_data.user_id)
            if not user.first_name:
                user.first_name = person_data.name
                user.save(update_fields=["first_name"])
        TabPerson.objects.create(
            tab=tab,
            name=person_data.name,
            user=user,
        )

    _sync_contacts_for_tab(tab)

    # Refresh to get related people
    tab.refresh_from_db()
    tab = Tab.objects.prefetch_related(
        'people__user',
        'settlements__from_person__user',
        'settlements__to_person__user__payment_methods'
    ).get(id=tab.id)

    safe_capture(request.auth.uuid, "tab_created", properties={
        "tab_id": str(tab.uuid),
        "people_count": len(payload.people),
        "default_currency": payload.default_currency,
        "settlement_currency": payload.settlement_currency,
    })

    return tab


@tab_router.get("/", response=CursorPageSchema[TabListSchema])
def list_tabs(request, cursor: str = None, archived: bool = False):
    """List the user's standalone tabs (one entry per tab).

    House periods are deliberately excluded — they belong to a house (TabGroup)
    and are surfaced via ``GET /groups/`` and ``GET /groups/{id}/periods``. Pass
    ?archived=true to list archived (deleted) tabs instead of active ones.
    """
    qs = (
        Tab.objects.accessible_by(request.auth)
        .filter(is_archived=archived, group__isnull=True)
    )
    qs = _annotate_tab_list(qs)
    items, next_cursor = _apply_tab_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


@tab_router.post("/demo", response=TabSchema)
@transaction.atomic
def create_demo_tab(request):
    """Create a pre-populated demo tab for the authenticated user."""
    tab = _create_demo_tab(request.auth)
    tab = Tab.objects.prefetch_related(
        'people__user',
        'bills__line_items__person_claims',
        'settlements__from_person__user',
        'settlements__to_person__user__payment_methods',
    ).get(id=tab.id)
    return tab


@tab_router.get("/contacts", response=List[ContactSchema])
def list_contacts(request, exclude_tab: str = None):
    """List the authenticated user's contacts, optionally excluding those already on a tab"""
    contacts = Contact.objects.filter(owner=request.auth).select_related('contact_user')
    if exclude_tab:
        tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=exclude_tab)
        existing_user_ids = TabPerson.objects.filter(
            tab=tab, user__isnull=False
        ).values_list('user_id', flat=True)
        contacts = contacts.exclude(contact_user_id__in=existing_user_ids)
    return list(contacts)


@tab_router.get("/{tab_id}", response=TabSchema)
def retrieve_tab(request, tab_id: str):
    """Retrieve a tab with all its people"""
    user_owes_sq = (
        PersonLineItemClaim.objects
        .filter(person__tab=OuterRef('pk'), person__user=request.auth)
        .filter(line_item__bill__paid_by__isnull=False)
        .exclude(line_item__bill__paid_by__user=request.auth)
        .values('person__tab')
        .annotate(total=Sum('settlement_amount'))
        .values('total')
    )

    user_owed_sq = (
        PersonLineItemClaim.objects
        .filter(line_item__bill__tab=OuterRef('pk'), line_item__bill__paid_by__user=request.auth)
        .exclude(person__user=request.auth)
        .values('line_item__bill__tab')
        .annotate(total=Sum('settlement_amount'))
        .values('total')
    )

    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user',
            'bills__line_items',
            'settlements__from_person__user',
            'settlements__to_person__user__payment_methods'
        ).annotate(
            user_owes=Coalesce(Subquery(user_owes_sq), 0, output_field=IntegerField()),
            user_owed=Coalesce(Subquery(user_owed_sq), 0, output_field=IntegerField()),
        ),
        uuid=tab_id,
    )

    return tab


@tab_router.patch("/{tab_id}", response=TabSchema)
@transaction.atomic
def update_tab(request, tab_id: str, payload: TabUpdateSchema):
    """Update tab fields (settlement_currency)"""
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user',
            'bills__line_items',
            'settlements__from_person__user',
            'settlements__to_person__user__payment_methods'
        ),
        uuid=tab_id,
    )

    if payload.settlement_currency is not None and payload.settlement_currency != tab.settlement_currency:
        new_currency = payload.settlement_currency

        claims = (
            PersonLineItemClaim.objects
            .filter(line_item__bill__tab=tab)
            .select_related('line_item__bill')
        )

        updated_claims = []
        for claim in claims:
            if claim.calculated_amount is None:
                continue
            try:
                claim.settlement_amount = convert_amount(
                    claim.calculated_amount,
                    claim.line_item.bill.currency,
                    new_currency,
                )
            except ExchangeRateNotFoundError as e:
                sentry_sdk.capture_exception(e)
                logger.error(
                    "Exchange rate missing during settlement_currency update: "
                    "tab=%s from=%s to=%s error=%s",
                    tab.uuid, claim.line_item.bill.currency, new_currency, e,
                )
                safe_capture(request.auth.uuid, "currency_conversion_failed", properties={
                    "tab_id": str(tab.uuid),
                    "from_currency": claim.line_item.bill.currency,
                    "to_currency": new_currency,
                    "context": "update_tab",
                })
                raise HttpError(422, f"Exchange rate not available: {e}")
            updated_claims.append(claim)

        PersonLineItemClaim.objects.bulk_update(updated_claims, ['settlement_amount'])
        tab.settlement_currency = new_currency

    tab.save()

    # Re-fetch with prefetches for serialization
    tab = Tab.objects.prefetch_related(
        'people__user',
        'bills__line_items',
        'settlements__from_person__user',
        'settlements__to_person__user__payment_methods'
    ).get(id=tab.id)

    return tab


@tab_router.delete("/{tab_id}")
def delete_tab(request, tab_id: str):
    """Hard-delete a tab. Only allowed for demo tabs — real tabs must be archived instead."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    if not tab.is_demo:
        raise HttpError(400, "Only demo tabs can be deleted. Archive the tab instead.")
    tab.delete()
    return {"success": True}


@tab_router.post("/{tab_id}/close", response=TabSchema)
@transaction.atomic
def close_tab(request, tab_id: str):
    """Close a tab (prevents adding new bills or splits) and close all bills"""
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user',
            'bills__line_items',
            'settlements__from_person__user',
            'settlements__to_person__user__payment_methods'
        ),
        uuid=tab_id,
    )

    _close_tab(tab, request.auth)

    # Refresh to get updated data
    return _serialize_tab(tab.id)


@tab_router.post("/{tab_id}/settle-period", response=TabSchema)
@transaction.atomic
def settle_period(request, tab_id: str):
    """Settle the current period of a house and open a fresh one.

    Closes this tab (snapshotting its total and freezing it) and spawns a new
    period Tab in the same house with the roster copied in. Returns the new
    active period. Only valid for tabs that belong to a house.
    """
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).select_related('group'),
        uuid=tab_id,
    )
    if tab.group_id is None:
        raise HttpError(400, "This tab is not part of a house")
    if tab.is_settled:
        raise HttpError(400, "This period is already settled")

    # Lock the period row so two concurrent rolls can't both spawn a new period.
    Tab.objects.select_for_update().filter(id=tab.id).first()

    group = tab.group
    next_index = (tab.period_index or 1) + 1

    _close_tab(tab, request.auth)
    new_tab = _create_period_tab(group, base_name=tab.name, period_index=next_index)

    safe_capture(request.auth.uuid, "period_rolled", properties={
        "group_id": str(group.uuid),
        "closed_tab_id": str(tab.uuid),
        "new_tab_id": str(new_tab.uuid),
        "period_index": next_index,
    })

    return _serialize_tab(new_tab.id)


@tab_router.post("/{tab_id}/archive")
def archive_tab(request, tab_id: str):
    """Soft-delete a tab — hides it from the tab list without affecting any data."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    tab.is_archived = True
    tab.save(update_fields=["is_archived"])

    safe_capture(request.auth.uuid, "tab_archived", properties={"tab_id": str(tab.uuid)})

    return {"success": True}


@tab_router.post("/{tab_id}/simplify", response=SimplifyResultSchema)
@transaction.atomic
def simplify_tab(request, tab_id: str):
    """
    Calculate and save simplified settlements for a tab.
    Settlements are calculated in the tab's settlement_currency, converting bills as needed.
    Tab remains open and settlements can be regenerated.
    """
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth),
        uuid=tab_id,
    )

    # Lock the tab row to prevent concurrent simplify calls
    Tab.objects.select_for_update().filter(id=tab.id).first()

    # Re-fetch with prefetch after lock
    tab = Tab.objects.prefetch_related(
        'people__user', 'bills__line_items__person_claims__person'
    ).get(id=tab.id)

    # Check if there are any non-archived bills
    bills = tab.bills.exclude(status=BillStatus.ARCHIVED)
    if not bills.exists():
        raise HttpError(400, "Tab has no bills to simplify")

    # Use tab's settlement_currency for settlements
    settlement_currency = tab.settlement_currency

    # Delete existing settlements for this tab (replace existing behavior)
    Settlement.objects.filter(tab=tab).delete()

    try:
        # Clear rate cache so lookups within this operation are cached but fresh
        clear_rate_cache()
        # Calculate simplified transactions with currency conversion
        transactions = simp_tab(tab, settlement_currency=settlement_currency)
    except ExchangeRateNotFoundError as e:
        safe_capture(request.auth.uuid, "currency_conversion_failed", properties={
            "tab_id": str(tab.uuid),
            "to_currency": settlement_currency,
            "context": "simplify_tab",
        })
        raise HttpError(400, f"Currency conversion failed: {str(e)}")

    # Create Settlement records
    settlements = []
    for txn in transactions:
        from_person = get_object_or_404(TabPerson, id=txn.payer_id, tab=tab)
        to_person = get_object_or_404(TabPerson, id=txn.payee_id, tab=tab)

        settlement = Settlement.objects.create(
            tab=tab,
            from_person=from_person,
            to_person=to_person,
            amount=txn.amount,
            currency=settlement_currency
        )
        settlements.append(settlement)

    # Prefetch related data for response
    settlements = Settlement.objects.filter(tab=tab).select_related(
        'from_person__user', 'to_person__user'
    ).prefetch_related('to_person__user__payment_methods')

    safe_capture(request.auth.uuid, "tab_simplified", properties={
        "tab_id": str(tab.uuid),
        "settlement_count": len(settlements),
        "settlement_currency": settlement_currency,
    })

    return {
        "settlements": list(settlements),
        "message": f"Created {len(settlements)} simplified settlement(s) in {settlement_currency}"
    }


@tab_router.post("/settlements/{settlement_id}/mark-paid", response=SettlementSchema)
@transaction.atomic
def mark_settlement_paid(request, settlement_id: str):
    """Mark a settlement as paid"""
    settlement = get_object_or_404(
        Settlement.objects.select_related(
            'tab', 'from_person__user', 'to_person__user'
        ).prefetch_related('to_person__user__payment_methods'),
        uuid=settlement_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    settlement.paid = True
    settlement.save()

    safe_capture(request.auth.uuid, "settlement_marked_paid", properties={
        "tab_id": str(settlement.tab.uuid),
        "amount_minor_units": settlement.amount,
        "currency": settlement.currency,
    })

    return settlement


@tab_router.get("/{tab_id}/person-totals", response=List[PersonSpendingTotalSchema])
def get_tab_person_totals(request, tab_id: str):
    """Get total spending per person for a tab in settlement currency"""

    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)

    totals = (
        PersonLineItemClaim.objects
        .filter(line_item__bill__tab=tab)
        .exclude(line_item__bill__status=BillStatus.ARCHIVED)
        .values('person__uuid', 'person__name')
        .annotate(total=Sum('settlement_amount'))
    )

    return [
        {
            'person_id': str(row['person__uuid']),
            'person_name': row['person__name'],
            'total': row['total'] or 0,
            'currency': tab.settlement_currency,
        }
        for row in totals
    ]


@tab_router.get("/invite/{invite_code}", response=InviteTabInfoSchema, auth=None)
def get_invite(request, invite_code: str):
    """Get tab info for invite page — no auth required"""
    tab = get_object_or_404(Tab, invite_code=invite_code)
    unclaimed = list(tab.people.filter(user__isnull=True))

    user_already_on_tab = False
    user = JWTBearer()(request)
    if user:
        user_already_on_tab = tab.people.filter(user=user).exists()

    return {
        "tab_id": str(tab.uuid),
        "tab_name": tab.name,
        "people": unclaimed,
        "user_already_on_tab": user_already_on_tab,
    }


@tab_router.post("/{tab_id}/people", response=TabPersonSchema)
@transaction.atomic
def add_tab_person(request, tab_id: str, payload: TabPersonCreateSchema):
    """Add a new person to a tab"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id, is_settled=False)
    user = None
    if payload.user_id:
        user = get_object_or_404(User, uuid=payload.user_id)
    person = TabPerson.objects.create(tab=tab, name=payload.name, user=user)
    if user:
        _sync_contacts_for_tab(tab)
    return person


@tab_router.patch("/{tab_id}/people/{person_id}", response=TabPersonSchema)
def update_tab_person(request, tab_id: str, person_id: str, payload: TabPersonUpdateSchema):
    """Update a person on a tab (currently only their name)"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id, is_settled=False)
    person = get_object_or_404(TabPerson, uuid=person_id, tab=tab)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HttpError(400, "Name cannot be empty")
        if tab.people.exclude(uuid=person.uuid).filter(name=name).exists():
            raise HttpError(400, "A person with that name already exists on this tab")
        person.name = name
        person.save(update_fields=["name", "updated_at"])
    return person


@tab_router.delete("/{tab_id}/people/{person_id}")
def remove_tab_person(request, tab_id: str, person_id: str):
    """Remove a person from a tab (only if not referenced in any bills or settlements)"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    person = get_object_or_404(TabPerson, uuid=person_id, tab=tab)
    if (PersonLineItemClaim.objects.filter(person=person).exists() or
            Bill.objects.filter(Q(paid_by=person) | Q(creator=person)).exists() or
            Settlement.objects.filter(Q(from_person=person) | Q(to_person=person)).exists()):
        raise HttpError(400, "Cannot remove a person who is associated with a bill or settlement")
    person.delete()
    return {"success": True}


@tab_router.post("/invite/{invite_code}/claim", response=MagicLinkSuccessSchema, auth=None)
@transaction.atomic
def claim_invite(request, invite_code: str, payload: ClaimInviteSchema):
    """Claim a placeholder person on a tab and send a magic link — no auth required"""
    tab = get_object_or_404(Tab, invite_code=invite_code)
    person = get_object_or_404(TabPerson, uuid=payload.person_id, tab=tab, user__isnull=True)

    # Prevent claiming if the authenticated user is already on this tab
    authed_user = JWTBearer()(request)
    if authed_user and tab.people.filter(user=authed_user).exists():
        raise HttpError(400, "You are already on this tab")

    user, _ = User.objects.get_or_create(email=payload.email.lower(), defaults={"username": payload.email.lower()})

    # Prevent claiming if the email's user is already on this tab
    if tab.people.filter(user=user).exists():
        raise HttpError(400, "This email is already associated with someone on this tab")
    if not user.first_name:
        user.first_name = person.name
        user.save(update_fields=["first_name"])
    person.user = user
    person.save()
    _sync_contacts_for_tab(tab)

    safe_capture(user.uuid, "invite_claimed", properties={"tab_id": str(tab.uuid)})

    return {"success": True}


@tab_router.post("/{tab_id}/upload-receipt")
def upload_receipt(request, tab_id: str, file: UploadedFile = File(...)):
    """Upload a receipt image, run OCR, and return parsed annotation."""
    from ninjatab.tabs.receipt_service import (
        validate_upload, upload_to_spaces, scan_receipt,
        check_scan_limit, increment_scan_count, ScanLimitExceeded,
    )

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

    image_key = upload_to_spaces(file, tab_id)

    try:
        result = scan_receipt(image_key, tab)
    except Exception as e:
        safe_capture(request.auth.uuid, "receipt_scan_failed", properties={
            "tab_id": str(tab.uuid),
            "reason": "exception",
            "exception_type": type(e).__name__,
        })
        raise

    increment_scan_count(tab)

    scan_metrics = result.pop("_scan_metrics", {}) or {}

    if result.get("document_annotation") is None:
        safe_capture(request.auth.uuid, "receipt_scan_failed", properties={
            "tab_id": str(tab.uuid),
            "reason": "ocr_empty",
            **scan_metrics,
        })
    else:
        safe_capture(
            request.auth.uuid,
            "receipt_scanned",
            properties=scan_metrics,
        )
        # Dedicated events for the two signals worth dashboarding directly.
        if scan_metrics.get("currency_source") in {"fallback_missing", "fallback_unsupported"}:
            safe_capture(
                request.auth.uuid,
                "receipt_currency_fallback",
                properties=scan_metrics,
            )
        if scan_metrics.get("items_match_receipt_total") is False:
            safe_capture(
                request.auth.uuid,
                "receipt_totals_mismatch",
                properties=scan_metrics,
            )

    result["scan_session_id"] = image_key
    return result


@tab_router.post("/scan-outcome")
def scan_outcome(request, payload: ScanOutcomeSchema):
    """Record a non-submit terminal outcome for a receipt scan.

    Always returns 200; failures are logged but never surfaced to the client.
    """
    from ninjatab.tabs.scan_analytics import fire_scan_outcome

    if payload.outcome not in {"rescanned", "abandoned"}:
        logger.warning("scan_outcome got invalid outcome=%s", payload.outcome)
        return {"ok": True}

    fire_scan_outcome(request.auth.uuid, payload.scan_session_id, payload.outcome)
    return {"ok": True}


@tab_router.post("/{tab_id}/upgrade")
@transaction.atomic
def upgrade_tab(request, tab_id: str):
    """Upgrade a tab to Pro"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    if tab.is_pro:
        raise HttpError(400, "Tab is already Pro")
    tab.is_pro = True
    tab.save(update_fields=["is_pro"])

    safe_capture(request.auth.uuid, "tab_upgraded", properties={"tab_id": str(tab.uuid)})

    return {"success": True}


@tab_router.get("/{tab_id}/can-add-single")
def can_add_single(request, tab_id: str):
    """Return 200 if a single expense can be added, 402 if limit reached."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    try:
        check_bill_limit(tab)
    except HttpError:
        safe_capture(request.auth.uuid, "bill_limit_hit", properties={"tab_id": str(tab.uuid)})
        raise
    return {"ok": True}


@tab_router.get("/{tab_id}/can-add-itemised")
def can_add_itemised(request, tab_id: str):
    """Return 200 if an itemised bill can be added, 402 if limit reached."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    try:
        check_bill_limit(tab)
    except HttpError:
        safe_capture(request.auth.uuid, "bill_limit_hit", properties={"tab_id": str(tab.uuid)})
        raise
    try:
        check_itemised_limit(tab)
    except HttpError:
        safe_capture(request.auth.uuid, "itemised_limit_hit", properties={"tab_id": str(tab.uuid)})
        raise
    return {"ok": True}


# Bill Endpoints
@bill_router.post("/", response=BillSchema)
@transaction.atomic
def create_bill(request, payload: BillCreateSchema):
    """Create a new bill with line items"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=payload.tab_id)
    creator = get_object_or_404(TabPerson, tab=tab, user=request.auth)

    # Idempotent replay: a retried create carrying a previously-seen client_id
    # returns the original bill rather than creating a duplicate (and skips the
    # limit checks / analytics below, which already ran for the first attempt).
    if payload.client_id:
        existing = Bill.objects.filter(
            creator=creator, client_id=payload.client_id
        ).first()
        if existing:
            return existing

    try:
        check_bill_limit(tab)
    except HttpError:
        safe_capture(request.auth.uuid, "bill_limit_hit", properties={"tab_id": str(tab.uuid)})
        raise
    if len(payload.line_items) > 1:
        try:
            check_itemised_limit(tab)
        except HttpError:
            safe_capture(request.auth.uuid, "itemised_limit_hit", properties={"tab_id": str(tab.uuid)})
            raise
    if len(payload.line_items) > 150:
        raise HttpError(400, "A bill cannot have more than 150 line items")

    paid_by = None
    if payload.paid_by_id:
        paid_by = get_object_or_404(TabPerson, uuid=payload.paid_by_id, tab=tab)

    try:
        with transaction.atomic():
            bill = Bill.objects.create(
                tab=tab,
                description=payload.description,
                currency=payload.currency,
                creator=creator,
                paid_by=paid_by,
                date=payload.date if payload.date else date.today(),
                receipt_image_key=payload.receipt_image_key,
                client_id=payload.client_id or None,
            )

            # Create line items with splits
            for line_item_data in payload.line_items:
                line_item = LineItem.objects.create(
                    bill=bill,
                    description=line_item_data.description,
                    translated_name=line_item_data.translated_name,
                    value=line_item_data.value,
                    split_type=line_item_data.split_type
                )

                # Create person claims if provided
                if line_item_data.person_splits:
                    _create_person_claims(line_item, line_item_data.person_splits, tab, user_uuid=str(request.auth.uuid))
    except IntegrityError:
        # A concurrent retry slipped past the check above; the unique constraint
        # caught it — return the bill that won the race.
        return Bill.objects.get(creator=creator, client_id=payload.client_id)

    safe_capture(request.auth.uuid, "bill_created", properties={
        "tab_id": str(tab.uuid),
        "bill_id": str(bill.uuid),
        "line_item_count": len(payload.line_items),
        "currency": payload.currency,
    })

    if payload.scan_session_id:
        try:
            from ninjatab.tabs.scan_analytics import compute_submit_outcome, fire_scan_outcome
            outcome = compute_submit_outcome(
                bool(payload.was_edited),
                bool(payload.had_mismatch),
            )
            fire_scan_outcome(
                request.auth.uuid,
                payload.scan_session_id,
                outcome,
                tab_id=tab.uuid,
                bill_id=bill.uuid,
            )
        except Exception:
            logger.exception("scan outcome emission failed for bill=%s", bill.uuid)

    return bill


def _create_person_claims(line_item: LineItem, person_splits: List[PersonSplitCreateSchema], tab: Tab, user_uuid: str = None):
    """Helper to create PersonLineItemClaim records"""
    total_shares = 0

    # Calculate total shares if needed
    if line_item.split_type == SplitType.SHARES:
        total_shares = sum(ps.split_value for ps in person_splits if ps.split_value)

    for person_split in person_splits:
        person = get_object_or_404(TabPerson, uuid=person_split.person_id, tab=tab)

        calculated_amount = None

        # Calculate the actual amount if split_value is provided
        if person_split.split_value is not None:
            if line_item.split_type == SplitType.SHARES:
                if total_shares > 0:
                    calculated_amount = round(line_item.value * person_split.split_value / total_shares)
                else:
                    calculated_amount = 0
            else:  # VALUE — split_value is already minor units
                calculated_amount = person_split.split_value

        settlement_amount = None
        if calculated_amount is not None:
            try:
                settlement_amount = convert_amount(
                    calculated_amount,
                    line_item.bill.currency,
                    tab.settlement_currency,
                )
            except ExchangeRateNotFoundError:
                settlement_amount = None
                if user_uuid:
                    safe_capture(user_uuid, "currency_conversion_failed", properties={
                        "tab_id": str(tab.uuid),
                        "from_currency": line_item.bill.currency,
                        "to_currency": tab.settlement_currency,
                        "context": "claim_calculation",
                    })

        PersonLineItemClaim.objects.create(
            person=person,
            line_item=line_item,
            split_value=person_split.split_value,
            calculated_amount=calculated_amount,
            settlement_amount=settlement_amount,
        )


@bill_router.post("/{bill_id}/submit-splits", response=BillSchema)
@transaction.atomic
def submit_bill_splits(request, bill_id: str, payload: BillSplitSubmitSchema):
    """Submit or update splits for a bill from the UI"""
    bill = get_object_or_404(
        Bill.objects.prefetch_related('line_items', 'tab__people'),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )

    if bill.tab.is_settled:
        raise HttpError(400, "Cannot edit a bill from a settled tab")

    if str(bill.uuid) != payload.bill_id:
        return {"error": "Bill ID mismatch"}, 400

    # Process each line item split
    for line_item_split in payload.line_item_splits:
        line_item = get_object_or_404(
            LineItem,
            uuid=line_item_split.line_item_id,
            bill=bill
        )

        # Delete existing claims for this line item
        PersonLineItemClaim.objects.filter(line_item=line_item).delete()

        # Create new claims
        _create_person_claims(line_item, line_item_split.person_splits, bill.tab, user_uuid=str(request.auth.uuid))

    safe_capture(request.auth.uuid, "bill_splits_submitted", properties={
        "tab_id": str(bill.tab.uuid),
        "bill_id": str(bill.uuid),
        "line_item_count": len(payload.line_item_splits),
    })

    # Splits live on child claim rows, so the bill row isn't otherwise touched —
    # bump its version explicitly so clients see the change for conflict detection.
    bill.save(update_fields=["version", "updated_at"])

    # Refresh the bill to get updated data
    bill.refresh_from_db()
    return bill


@bill_router.get("/", response=CursorPageSchema[BillListSchema])
def list_bills(request, tab_id: str = None, cursor: str = None):
    """List all bills, optionally filtered by tab"""
    qs = Bill.objects.filter(tab__in=Tab.objects.accessible_by(request.auth))
    if tab_id:
        qs = qs.filter(tab__uuid=tab_id)
    qs = qs.select_related('paid_by__user', 'tab').prefetch_related('line_items')
    items, next_cursor = _apply_bill_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


@bill_router.get("/details", response=CursorPageSchema[BillSchema])
def list_bill_details(request, tab_id: str = None, cursor: str = None):
    """List bills with full detail (line items + claims), optionally filtered by tab.

    Mirrors list_bills' cursor pagination but returns the same payload as
    retrieve_bill for each item, so a client can warm its cache for a whole tab
    in one request instead of one fetch per bill.
    """
    qs = Bill.objects.filter(tab__in=Tab.objects.accessible_by(request.auth))
    if tab_id:
        qs = qs.filter(tab__uuid=tab_id)
    qs = qs.select_related('tab', 'creator__user', 'paid_by__user').prefetch_related(
        'line_items__person_claims__person__user'
    )
    items, next_cursor = _apply_bill_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


@bill_router.get("/{bill_id}", response=BillSchema)
def retrieve_bill(request, bill_id: str):
    """Retrieve a bill with all its line items and claims"""
    bill = get_object_or_404(
        Bill.objects.select_related('tab').prefetch_related(
            'line_items__person_claims__person__user',
            'creator__user',
            'paid_by__user'
        ),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    return bill


@bill_router.patch("/{bill_id}", response=BillSchema)
@transaction.atomic
def update_bill(request, bill_id: str, payload: BillUpdateSchema):
    """Update bill fields (description, currency, paid_by)"""
    bill = get_object_or_404(
        Bill.objects.select_related('tab').prefetch_related(
            'line_items__person_claims__person__user',
            'creator__user',
            'paid_by__user'
        ),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )

    if bill.tab.is_settled:
        raise HttpError(400, "Cannot edit a bill from a settled tab")

    # Update fields if provided
    if payload.description is not None:
        bill.description = payload.description

    if payload.currency is not None and payload.currency != bill.currency:
        new_currency = payload.currency
        settlement_currency = bill.tab.settlement_currency

        claims = (
            PersonLineItemClaim.objects
            .filter(line_item__bill=bill)
        )

        updated_claims = []
        for claim in claims:
            if claim.calculated_amount is None:
                continue
            try:
                claim.settlement_amount = convert_amount(
                    claim.calculated_amount,
                    new_currency,
                    settlement_currency,
                )
            except ExchangeRateNotFoundError as e:
                sentry_sdk.capture_exception(e)
                logger.error(
                    "Exchange rate missing during bill currency update: "
                    "bill=%s from=%s to=%s error=%s",
                    bill.uuid, new_currency, settlement_currency, e,
                )
                safe_capture(request.auth.uuid, "currency_conversion_failed", properties={
                    "tab_id": str(bill.tab.uuid),
                    "from_currency": new_currency,
                    "to_currency": settlement_currency,
                    "context": "update_bill",
                })
                raise HttpError(422, f"Exchange rate not available: {e}")
            updated_claims.append(claim)

        PersonLineItemClaim.objects.bulk_update(updated_claims, ['settlement_amount'])
        bill.currency = new_currency

    if payload.description is not None:
        bill.description = payload.description

    if payload.paid_by_id is not None:
        paid_by = get_object_or_404(TabPerson, uuid=payload.paid_by_id, tab=bill.tab)
        bill.paid_by = paid_by

    if payload.date is not None:
        bill.date = payload.date

    bill.save()
    bill.refresh_from_db()

    return bill



@bill_router.delete("/{bill_id}")
def delete_bill(request, bill_id: str):
    """Delete a bill"""
    bill = get_object_or_404(
        Bill.objects.select_related('tab'),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    if bill.tab.is_settled:
        raise HttpError(400, "Cannot delete a bill from a closed tab")

    tab_uuid = str(bill.tab.uuid)
    bill_uuid = str(bill.uuid)
    bill.delete()

    safe_capture(request.auth.uuid, "bill_deleted", properties={
        "tab_id": tab_uuid,
        "bill_id": bill_uuid,
    })

    return {"success": True}


# ---------------------------------------------------------------------------
# Houses (TabGroup) — ongoing tabs that retain people across settle-ups
# ---------------------------------------------------------------------------

def _tab_settlement_total(tab, settlement_currency):
    """Live total spend of a tab in `settlement_currency` (minor units).

    Returns (total, ok); ok is False if a currency conversion was unavailable.
    Requires `bills__line_items` to be prefetched on `tab`.
    """
    total = 0
    for bill in tab.bills.all():
        if bill.status == BillStatus.ARCHIVED.value:
            continue
        bill_total = sum((li.value or 0) for li in bill.line_items.all())
        if bill.currency != settlement_currency:
            try:
                bill_total = convert_amount(bill_total, bill.currency, settlement_currency)
            except ExchangeRateNotFoundError:
                return 0, False
        total += bill_total
    return total, True


def _reload_group(group_id):
    """Re-fetch a group with everything GroupDetailSchema needs prefetched."""
    return TabGroup.objects.prefetch_related(
        'members__user',
        'tabs__bills__line_items',
    ).get(id=group_id)


def _group_detail_payload(group):
    """Build the GroupDetailSchema dict, including spend aggregated across periods."""
    periods = sorted(group.tabs.all(), key=lambda t: t.created_at, reverse=True)
    current = None
    total = 0
    for p in periods:
        if p.is_settled:
            total += p.settlement_currency_settled_total or 0
        elif not p.is_archived:
            if current is None:
                current = p
            live, ok = _tab_settlement_total(p, group.settlement_currency)
            if ok:
                total += live

    return {
        "id": str(group.uuid),
        "name": group.name,
        "description": group.description,
        "default_currency": group.default_currency,
        "settlement_currency": group.settlement_currency,
        "invite_code": str(group.invite_code) if group.invite_code else None,
        "is_archived": group.is_archived,
        "members": list(group.members.all()),
        "current_period": current,
        "periods": periods,
        "group_total_spend": total,
        "group_total_spend_display": minor_to_decimal(total, group.settlement_currency),
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


@group_router.post("/", response=GroupDetailSchema)
@transaction.atomic
def create_group(request, payload: GroupCreateSchema):
    """Create a house with a roster and open its first period."""
    group = TabGroup.objects.create(
        name=payload.name,
        description=payload.description,
        default_currency=payload.default_currency,
        settlement_currency=payload.settlement_currency,
        created_by=request.auth,
    )

    for member_data in payload.members:
        user = None
        if member_data.user_id:
            user = get_object_or_404(User, uuid=member_data.user_id)
            if not user.first_name:
                user.first_name = member_data.name
                user.save(update_fields=["first_name"])
        TabGroupMember.objects.create(group=group, name=member_data.name, user=user)

    _create_period_tab(group, base_name=payload.name, period_index=1)

    safe_capture(request.auth.uuid, "group_created", properties={
        "group_id": str(group.uuid),
        "member_count": len(payload.members),
        "settlement_currency": payload.settlement_currency,
    })

    return _group_detail_payload(_reload_group(group.id))


@group_router.get("/", response=CursorPageSchema[GroupListSchema])
def list_groups(request, cursor: str = None, archived: bool = False):
    """List houses. Pass ?archived=true to list archived houses instead."""
    qs = TabGroup.objects.accessible_by(request.auth).filter(is_archived=archived).annotate(
        member_count=Count('members', distinct=True),
        period_count=Count('tabs', distinct=True),
    )
    items, next_cursor = _apply_tab_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


@group_router.get("/{group_id}", response=GroupDetailSchema)
def retrieve_group(request, group_id: str):
    """Retrieve a house with its roster, current period, and period history."""
    group = get_object_or_404(TabGroup.objects.accessible_by(request.auth), uuid=group_id)
    return _group_detail_payload(_reload_group(group.id))


@group_router.get("/{group_id}/periods", response=CursorPageSchema[TabListSchema])
def list_group_periods(request, group_id: str, cursor: str = None):
    """List a house's periods (current + settled history), newest first.

    Returns the same TabListSchema the individual-tabs list uses, so the client
    can reuse its tab-row rendering for period history.
    """
    group = get_object_or_404(TabGroup.objects.accessible_by(request.auth), uuid=group_id)
    qs = _annotate_tab_list(group.tabs.all())
    items, next_cursor = _apply_tab_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


@group_router.post("/{group_id}/members", response=GroupDetailSchema)
@transaction.atomic
def add_group_member(request, group_id: str, payload: GroupMemberCreateSchema):
    """Add a member to a house. Also projects them into the current open period."""
    group = get_object_or_404(TabGroup.objects.accessible_by(request.auth), uuid=group_id)
    name = payload.name.strip()
    if not name:
        raise HttpError(400, "Name cannot be empty")
    if group.members.filter(name=name).exists():
        raise HttpError(400, "A member with that name already exists in this house")

    user = None
    if payload.user_id:
        user = get_object_or_404(User, uuid=payload.user_id)
    member = TabGroupMember.objects.create(group=group, name=name, user=user)

    current = group.current_period
    if current and not current.people.filter(name=name).exists():
        TabPerson.objects.create(tab=current, name=name, user=user, member=member)
        if user:
            _sync_contacts_for_tab(current)

    return _group_detail_payload(_reload_group(group.id))


@group_router.delete("/{group_id}/members/{member_id}")
@transaction.atomic
def remove_group_member(request, group_id: str, member_id: str):
    """Remove a member from a house (stops projecting them into future periods).

    Historical periods keep their person rows. The current open period's person
    is detached too, unless they're already on a bill or settlement there.
    """
    group = get_object_or_404(TabGroup.objects.accessible_by(request.auth), uuid=group_id)
    member = get_object_or_404(TabGroupMember, uuid=member_id, group=group)

    current = group.current_period
    if current:
        person = current.people.filter(member=member).first()
        if person:
            referenced = (
                PersonLineItemClaim.objects.filter(person=person).exists()
                or Bill.objects.filter(Q(paid_by=person) | Q(creator=person)).exists()
                or Settlement.objects.filter(Q(from_person=person) | Q(to_person=person)).exists()
            )
            if referenced:
                raise HttpError(400, "Cannot remove a member who is already on a bill or settlement in the current period")
            person.delete()

    member.delete()
    return {"success": True}


@group_router.get("/invite/{invite_code}", response=GroupInviteInfoSchema, auth=None)
def get_group_invite(request, invite_code: str):
    """Get house info for an invite page — no auth required."""
    group = get_object_or_404(TabGroup, invite_code=invite_code)
    unclaimed = list(group.members.filter(user__isnull=True))

    user_already_member = False
    user = JWTBearer()(request)
    if user:
        user_already_member = group.members.filter(user=user).exists()

    return {
        "group_id": str(group.uuid),
        "group_name": group.name,
        "members": unclaimed,
        "user_already_member": user_already_member,
    }


@group_router.post("/invite/{invite_code}/claim", response=MagicLinkSuccessSchema, auth=None)
@transaction.atomic
def claim_group_invite(request, invite_code: str, payload: ClaimGroupInviteSchema):
    """Claim a placeholder member of a house — no auth required."""
    group = get_object_or_404(TabGroup, invite_code=invite_code)
    member = get_object_or_404(TabGroupMember, uuid=payload.member_id, group=group, user__isnull=True)

    authed_user = JWTBearer()(request)
    if authed_user and group.members.filter(user=authed_user).exists():
        raise HttpError(400, "You are already a member of this house")

    user, _ = User.objects.get_or_create(
        email=payload.email.lower(), defaults={"username": payload.email.lower()}
    )
    if group.members.filter(user=user).exists():
        raise HttpError(400, "This email is already associated with a member of this house")
    if not user.first_name:
        user.first_name = member.name
        user.save(update_fields=["first_name"])
    member.user = user
    member.save()

    # Carry the claim through to the current open period's projected person.
    current = group.current_period
    if current:
        person = current.people.filter(member=member, user__isnull=True).first()
        if person:
            person.user = user
            person.save(update_fields=["user", "updated_at"])
            _sync_contacts_for_tab(current)

    safe_capture(user.uuid, "group_invite_claimed", properties={"group_id": str(group.uuid)})
    return {"success": True}


