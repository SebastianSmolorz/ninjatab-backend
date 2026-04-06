import base64
import uuid
from datetime import datetime
from decimal import Decimal

from ninja import Router, UploadedFile, File
from ninja.errors import HttpError
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Q, Count, OuterRef, Subquery, Sum, DecimalField
from django.db.models.functions import Coalesce
from django.contrib.auth import get_user_model

from ninjatab.tabs.models import *
from ninjatab.tabs.schemas import *
from ninjatab.tabs.simp import simp_tab
from ninjatab.currencies.exchange import convert_amount, ExchangeRateNotFoundError, clear_rate_cache
from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.schemas import MagicLinkSuccessSchema
from ninjatab.auth.jwt_utils import create_magic_token
from ninjatab.auth.email import send_magic_link
from ninjatab.tabs.limits import check_bill_limit, check_itemised_limit

User = get_user_model()

tab_router = Router(tags=["tabs"], auth=JWTBearer())
bill_router = Router(tags=["bills"], auth=JWTBearer())

import logging
import sentry_sdk

logger = logging.getLogger("app")

PAGE_SIZE = 25


CURSOR_ORDER = '-created_at,-id'


def _apply_cursor(qs, cursor: str | None):
    """
    Apply cursor-based pagination to a queryset.
    Orders by (-created_at, -id). Cursor is base64-encoded "order|created_at|id".
    The order contract is embedded in the cursor and validated on decode.
    Returns (page_items, next_cursor).
    """
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor).decode()
            order, ts_str, obj_id = decoded.split('|', 2)
            if order != CURSOR_ORDER:
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
    items = list(qs[:PAGE_SIZE + 1])

    next_cursor = None
    if len(items) > PAGE_SIZE:
        items = items[:PAGE_SIZE]
        last = items[-1]
        raw = f"{CURSOR_ORDER}|{last.created_at.isoformat()}|{last.id}"
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
        'settlements__to_person__user'
    ).get(id=tab.id)

    return tab


@tab_router.get("/", response=CursorPageSchema[TabListSchema])
def list_tabs(request, cursor: str = None):
    """List all tabs"""
    qs = Tab.objects.accessible_by(request.auth).annotate(
        bill_count=Count('bills', distinct=True),
        people_count=Count('people', distinct=True),
    )
    items, next_cursor = _apply_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


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
            'settlements__to_person__user'
        ).annotate(
            user_owes=Coalesce(Subquery(user_owes_sq), 0, output_field=DecimalField()),
            user_owed=Coalesce(Subquery(user_owed_sq), 0, output_field=DecimalField()),
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
            'settlements__to_person__user'
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
        'settlements__to_person__user'
    ).get(id=tab.id)

    return tab


@tab_router.delete("/{tab_id}")
def delete_tab(request, tab_id: str):
    """Delete a tab"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
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
            'settlements__to_person__user'
        ),
        uuid=tab_id,
    )
    # Prevent settling a tab with no bills
    bills = list(tab.bills.prefetch_related('line_items').exclude(status=BillStatus.ARCHIVED.value))
    if not bills:
        raise HttpError(400, "Cannot settle a tab with no bills")

    # Snapshot total spent in settlement currency
    total = Decimal('0')
    for bill in bills:
        bill_total = sum((li.value or Decimal('0')) for li in bill.line_items.all())
        if bill.currency != tab.settlement_currency:
            bill_total = convert_amount(bill_total, bill.currency, tab.settlement_currency)
        total += bill_total

    tab.is_settled = True
    tab.settlement_currency_settled_total = total
    tab.save()

    # Refresh to get updated data
    tab.refresh_from_db()
    tab = Tab.objects.prefetch_related(
        'people__user',
        'bills__line_items',
        'settlements__from_person__user',
        'settlements__to_person__user'
    ).get(id=tab.id)

    return tab


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
    settlements = Settlement.objects.filter(tab=tab).select_related('from_person__user', 'to_person__user')

    return {
        "settlements": list(settlements),
        "message": f"Created {len(settlements)} simplified settlement(s) in {settlement_currency}"
    }


@tab_router.post("/settlements/{settlement_id}/mark-paid", response=SettlementSchema)
@transaction.atomic
def mark_settlement_paid(request, settlement_id: str):
    """Mark a settlement as paid"""
    settlement = get_object_or_404(
        Settlement.objects.select_related('from_person__user', 'to_person__user'),
        uuid=settlement_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    settlement.paid = True
    settlement.save()
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
            'total': row['total'] or Decimal('0'),
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


@tab_router.delete("/{tab_id}/people/{person_id}")
def remove_tab_person(request, tab_id: str, person_id: str):
    """Remove a person from a tab (only if not referenced in any bills)"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    person = get_object_or_404(TabPerson, uuid=person_id, tab=tab)
    if (PersonLineItemClaim.objects.filter(person=person).exists() or
            Bill.objects.filter(Q(paid_by=person) | Q(creator=person)).exists()):
        raise HttpError(400, "Cannot remove a person who is associated with a bill")
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
        raise HttpError(429, str(e))

    try:
        validate_upload(file)
    except ValueError as e:
        raise HttpError(400, str(e))

    image_url = upload_to_spaces(file, tab_id)
    result = scan_receipt(image_url, tab_id)
    increment_scan_count(tab)
    return result


@tab_router.post("/{tab_id}/upgrade")
@transaction.atomic
def upgrade_tab(request, tab_id: str):
    """Upgrade a tab to Pro"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    if tab.is_pro:
        raise HttpError(400, "Tab is already Pro")
    tab.is_pro = True
    tab.save(update_fields=["is_pro"])
    return {"success": True}


@tab_router.get("/{tab_id}/can-add-single")
def can_add_single(request, tab_id: str):
    """Return 200 if a single expense can be added, 402 if limit reached."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    check_bill_limit(tab)
    return {"ok": True}


@tab_router.get("/{tab_id}/can-add-itemised")
def can_add_itemised(request, tab_id: str):
    """Return 200 if an itemised bill can be added, 402 if limit reached."""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=tab_id)
    check_bill_limit(tab)
    check_itemised_limit(tab)
    return {"ok": True}


# Bill Endpoints
@bill_router.post("/", response=BillSchema)
@transaction.atomic
def create_bill(request, payload: BillCreateSchema):
    """Create a new bill with line items"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), uuid=payload.tab_id)
    check_bill_limit(tab)
    if len(payload.line_items) > 1:
        check_itemised_limit(tab)
    if len(payload.line_items) > 150:
        raise HttpError(400, "A bill cannot have more than 150 line items")
    creator = get_object_or_404(TabPerson, tab=tab, user=request.auth)

    paid_by = None
    if payload.paid_by_id:
        paid_by = get_object_or_404(TabPerson, uuid=payload.paid_by_id, tab=tab)

    bill = Bill.objects.create(
        tab=tab,
        description=payload.description,
        currency=payload.currency,
        creator=creator,
        paid_by=paid_by,
        date=payload.date if payload.date else date.today(),
        receipt_image_url=payload.receipt_image_url
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
            _create_person_claims(line_item, line_item_data.person_splits, tab)

    return bill


def _create_person_claims(line_item: LineItem, person_splits: List[PersonSplitCreateSchema], tab: Tab):
    """Helper to create PersonLineItemClaim records"""
    total_shares = Decimal(0)

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
                    calculated_amount = (line_item.value * person_split.split_value) / total_shares
                else:
                    calculated_amount = Decimal(0)
            else:  # VALUE
                calculated_amount = person_split.split_value

            if calculated_amount is not None:
                calculated_amount = calculated_amount.quantize(Decimal('0.01'))

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
        _create_person_claims(line_item, line_item_split.person_splits, bill.tab)

    # Refresh the bill to get updated data
    bill.refresh_from_db()
    return bill


@bill_router.get("/", response=CursorPageSchema[BillListSchema])
def list_bills(request, tab_id: str = None, cursor: str = None):
    """List all bills, optionally filtered by tab"""
    qs = Bill.objects.filter(tab__in=Tab.objects.accessible_by(request.auth))
    if tab_id:
        qs = qs.filter(tab__uuid=tab_id)
    qs = qs.select_related('paid_by__user').prefetch_related('line_items')
    items, next_cursor = _apply_cursor(qs, cursor)
    return {"items": items, "next_cursor": next_cursor}


@bill_router.get("/{bill_id}", response=BillSchema)
def retrieve_bill(request, bill_id: str):
    """Retrieve a bill with all its line items and claims"""
    bill = get_object_or_404(
        Bill.objects.prefetch_related(
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
        Bill.objects.prefetch_related(
            'line_items__person_claims__person__user',
            'creator__user',
            'paid_by__user'
        ),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )

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
    bill = get_object_or_404(Bill, uuid=bill_id, tab__in=Tab.objects.accessible_by(request.auth))
    if bill.tab.is_settled:
        raise HttpError(400, "Cannot delete a bill from a closed tab")
    bill.delete()
    return {"success": True}
