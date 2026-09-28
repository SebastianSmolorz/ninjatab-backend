import base64
import logging
from datetime import date
from typing import List

import sentry_sdk
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError

from ninjatab.auth.bearer import JWTBearer
from ninjatab.currencies.exchange import ExchangeRateNotFoundError, convert_amount
from ninjatab.tabs.limits import check_bill_limit, check_itemised_limit
from ninjatab.tabs.models import Bill, LineItem, PersonLineItemClaim, SplitType, Tab, TabPerson
from ninjatab.tabs.scan_analytics import compute_submit_outcome, fire_scan_outcome
from ninjatab.tabs.schemas import (
    BillCreateSchema, BillListSchema, BillSchema, BillSplitSubmitSchema,
    BillUpdateSchema, CursorPageSchema, PersonSplitCreateSchema,
)
from ninjatab.utilities.analytics import safe_capture

logger = logging.getLogger("app")

bill_router = Router(tags=["bills"], auth=JWTBearer())

PAGE_SIZE = 15
BILL_CURSOR_ORDER = '-date,-id'


def _check_version(obj, sent: int | None):
    """Optimistic-concurrency guard: reject a write made against stale data.

    `sent is None` means a client that predates conflict detection, which is
    allowed through unchanged (last write wins, as before).

    The message is worded to read sensibly both on its own and behind the older
    app's snackbar prefix ("Failed to save splits: ..."), since shipped builds
    surface `detail` verbatim and can't be changed.
    """
    # ponytail: bill-level version only, and check-then-save without
    # select_for_update — so two *simultaneous* writes can both pass. It catches
    # the real case (two devices, seconds apart), not a true race. Row-lock the
    # read if that ever matters. Per-line-item granularity needs
    # LineItem.version checks.
    if sent is not None and sent != obj.version:
        raise HttpError(409, "someone else changed this expense — reopen it to get the latest")



def _apply_bill_cursor(qs, cursor: str | None):
    """Cursor pagination for bills, ordered by (-date, -id)."""
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor).decode()
            order, date_str, obj_id = decoded.split('|', 2)
            if order != BILL_CURSOR_ORDER:
                raise ValueError("cursor order mismatch")
            cursor_date = date.fromisoformat(date_str)
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
                    split_type=line_item_data.split_type,
                    proportional=line_item_data.proportional,
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
    """Submit or update splits for a bill from the UI

    TODO (needs a forced update to minimum_app_version >= 1.3.0): fold
    BillUpdateSchema's fields into this endpoint so one save is one atomic
    request. Today the app's save is PATCH-then-submit-splits, so a third-party
    write landing between them applies the metadata and rejects the splits —
    the version guard catches it but can't undo the first half.

    Do this in the same cutover as making `version` required (see
    BillUpdateSchema.version); both are blocked on the same force-upgrade, and
    doing them together costs one forced update instead of two.

    Check first whether per-claim sync is still planned — it would obsolete this.
    That design writes each claim as it changes instead of batch-submitting them,
    which shrinks or retires this endpoint, so merging into it would be wasted
    work. It also collides with the delete-and-recreate below (a batch save wipes
    individually-synced rows; `_create_person_claims` would need to upsert on the
    (person, line_item) unique constraint), and it wants claim writes bumping
    LineItem.version rather than Bill.version — otherwise one person claiming an
    item conflicts with another editing the bill's currency. If per-claim sync is
    going ahead, spend the forced update on `version` alone and skip the merge.
    """
    bill = get_object_or_404(
        Bill.objects.prefetch_related('line_items', 'tab__people'),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )

    if bill.tab.is_settled:
        raise HttpError(400, "Cannot edit a bill from a settled tab")

    _check_version(bill, payload.version)

    if str(bill.uuid) != payload.bill_id:
        return {"error": "Bill ID mismatch"}, 400

    # Process each line item split
    for line_item_split in payload.line_item_splits:
        line_item = get_object_or_404(
            LineItem,
            uuid=line_item_split.line_item_id,
            bill=bill
        )

        # Persist the proportional flag (cleared when the user overrides to a
        # manual split). Only save on change to avoid a needless version bump.
        if line_item.proportional != line_item_split.proportional:
            line_item.proportional = line_item_split.proportional
            line_item.save(update_fields=['proportional'])

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
def list_bills(request, tab_id: str = None, cursor: str = None, mine: bool = False):
    """List all bills, optionally filtered by tab. With mine=true, restrict to
    bills the caller is involved in (paid for, or has a claim on)."""
    qs = Bill.objects.filter(tab__in=Tab.objects.accessible_by(request.auth))
    if tab_id:
        qs = qs.filter(tab__uuid=tab_id)
    if mine:
        # Both claim conditions sit in one Q so they match the *same* claim row:
        # the caller's own claim, and only when it actually owes something (a
        # zero-share claim still has a row but shouldn't count as "yours").
        qs = qs.filter(
            Q(paid_by__user=request.auth)
            | Q(
                line_items__person_claims__person__user=request.auth,
                line_items__person_claims__calculated_amount__gt=0,
            )
        ).distinct()
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


@bill_router.get("/{bill_id}/version")
def get_bill_version(request, bill_id: str):
    """Just the version counter, for the app's conflict heartbeat.

    Deliberately not retrieve_bill: this is polled every few seconds per open
    expense, so it must not ship a full bill payload (line items, claims, FX
    conversions) each time.
    """
    bill = get_object_or_404(
        Bill.objects.only('version'),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    return {"version": bill.version}


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

    _check_version(bill, payload.version)

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
def delete_bill(request, bill_id: str, version: int | None = None):
    """Delete a bill"""
    bill = get_object_or_404(
        Bill.objects.select_related('tab'),
        uuid=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    if bill.tab.is_settled:
        raise HttpError(400, "Cannot delete a bill from a closed tab")

    _check_version(bill, version)

    tab_uuid = str(bill.tab.uuid)
    bill_uuid = str(bill.uuid)
    bill.delete()

    safe_capture(request.auth.uuid, "bill_deleted", properties={
        "tab_id": tab_uuid,
        "bill_id": bill_uuid,
    })

    return {"success": True}

