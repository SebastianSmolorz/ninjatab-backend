import logging
from typing import List

import sentry_sdk
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.schemas import MagicLinkSuccessSchema
from ninjatab.currencies.exchange import ExchangeRateNotFoundError, clear_rate_cache, convert_amount
from ninjatab.tabs.demo import create_demo_tab as _create_demo_tab
from ninjatab.tabs.limits import check_bill_limit, check_itemised_limit
from ninjatab.tabs.models import Bill, BillStatus, Contact, PersonLineItemClaim, Settlement, Tab, TabPerson
from ninjatab.tabs.schemas import (
    ClaimInviteSchema, ContactSchema, CursorPageSchema, InviteTabInfoSchema,
    PersonSpendingTotalSchema, PublicTabSchema, SettlementSchema, SimplifyResultSchema,
    TabCreateSchema, TabListSchema, TabPersonCreateSchema, TabPersonSchema,
    TabPersonUpdateSchema, TabSchema, TabUpdateSchema, _receipt_image_url,
)
from ninjatab.tabs.simp import simp_tab
from ninjatab.utilities.analytics import safe_capture

from .common import _annotate_tab_list, _apply_tab_cursor, _create_period_tab, _sync_contacts_for_tab

User = get_user_model()
logger = logging.getLogger("app")

tab_router = Router(tags=["tabs"], auth=JWTBearer())


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


def _public_tab_payload(tab, presign=True):
    """Build the whitelisted payload for the public read-only tab view.

    Only fields listed here ever reach an unauthenticated caller — no emails,
    user ids, invite codes or settlements.
    """
    bills = [b for b in tab.bills.all() if b.status != BillStatus.ARCHIVED]
    settlement_currency = tab.settlement_currency

    group_spend = 0
    conversion_ok = True
    person_spend = {}

    bill_payloads = []
    for bill in bills:
        bill_total = 0
        person_totals = {}
        line_items = []
        for li in bill.line_items.all():
            bill_total += li.value or 0
            claims = []
            for claim in li.person_claims.all():
                claims.append({
                    'person_id': str(claim.person.uuid),
                    'person_name': claim.person.name,
                    'split_value': claim.split_value,
                    'amount': claim.calculated_amount,
                })
                person_totals[claim.person.uuid] = (
                    person_totals.get(claim.person.uuid, 0) + (claim.calculated_amount or 0)
                )
                person_spend[claim.person.uuid] = (
                    person_spend.get(claim.person.uuid, 0) + (claim.settlement_amount or 0)
                )
            line_items.append({
                'id': str(li.uuid),
                'description': li.description,
                'value': li.value,
                'split_type': li.split_type,
                'claims': claims,
            })

        if conversion_ok:
            try:
                converted = bill_total
                if bill.currency != settlement_currency:
                    converted = convert_amount(bill_total, bill.currency, settlement_currency)
                group_spend += converted
            except ExchangeRateNotFoundError:
                conversion_ok = False

        bill_payloads.append({
            'id': str(bill.uuid),
            'description': bill.description,
            'currency': bill.currency,
            'date': bill.date,
            'total_amount': bill_total,
            'created_by': bill.creator.name,
            'paid_by': bill.paid_by.name if bill.paid_by else None,
            # Presigning is skipped for the static export: the signature would
            # expire long before the file is regenerated.
            'receipt_image_url': _receipt_image_url(bill) if presign else '',
            'has_receipt': bool(getattr(bill, 'receipt_image_key', '') or getattr(bill, 'receipt_image_url', '')),
            'person_totals': [
                {'person_id': str(p.uuid), 'person_name': p.name, 'amount': person_totals[p.uuid]}
                for p in tab.people.all() if p.uuid in person_totals
            ],
            'line_items': line_items,
        })

    return {
        'id': str(tab.uuid),
        'name': tab.name,
        'description': tab.description,
        'settlement_currency': settlement_currency,
        'is_settled': tab.is_settled,
        'is_pro': tab.is_pro,
        'group_spend': group_spend if conversion_ok else None,
        'people': [
            {
                'id': str(p.uuid),
                'name': p.name,
                'spend': person_spend.get(p.uuid, 0),
            }
            for p in tab.people.all()
        ],
        'bills': bill_payloads,
        'settlements': [
            {
                'from_name': st.from_person.name,
                'to_name': st.to_person.name,
                'amount': st.amount,
                'currency': st.currency,
                'paid': st.paid,
            }
            for st in tab.settlements.all()
        ],
    }


@tab_router.get("/public/{slug}", response=PublicTabSchema, auth=None)
def retrieve_public_tab(request, slug: str):
    """Read-only tab view for tabs explicitly opted in via Tab.is_public.

    Addressed by the friendly `public_slug`, which only exists once a tab is
    published — so there is no uuid to guess or leak here at all.
    """
    # TEMPORARY — is_archived is deliberately NOT filtered here.
    #
    # `is_archived` is the app's soft-delete: hitting delete on a tab in the
    # app just sets this flag. A public tab is a marketing asset built by
    # someone in their own app, so if archiving also pulled it from the public
    # view, any tidy-up in their tab list would silently break a live shared
    # link. Until public tabs get their own lifecycle (an explicit unpublish,
    # or a snapshot decoupled from the owner's tab), archived public tabs keep
    # rendering. A real DB delete still removes it — nothing to serve.
    #
    # Revisit when public tabs stop being hand-curated demos.
    tab = get_object_or_404(
        Tab.objects.filter(is_public=True).prefetch_related(
            'people',
            'bills__creator',
            'bills__paid_by',
            'bills__line_items__person_claims__person',
            'settlements__from_person',
            'settlements__to_person',
        ),
        public_slug=slug,
    )
    return _public_tab_payload(tab)


@tab_router.get("/public/{slug}/receipt/{bill_id}", auth=None)
def public_bill_receipt(request, slug: str, bill_id: str):
    """Redirect to a freshly signed receipt image for a bill on a public tab.

    The public tab pages are exported to static files, and a presigned S3 URL
    expires — so the export stores `has_receipt` and links here instead. One
    hop, and the signature is always minutes old.
    """
    tab = get_object_or_404(Tab.objects.filter(is_public=True), public_slug=slug)
    bill = get_object_or_404(Bill.objects.filter(tab=tab).exclude(status=BillStatus.ARCHIVED), uuid=bill_id)
    url = _receipt_image_url(bill)
    if not url:
        raise HttpError(404, "No receipt for this bill")
    return HttpResponseRedirect(url)


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

