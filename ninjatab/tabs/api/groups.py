from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError

from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.schemas import MagicLinkSuccessSchema
from ninjatab.currencies.currency_utils import minor_to_decimal
from ninjatab.currencies.exchange import ExchangeRateNotFoundError, convert_amount
from ninjatab.tabs.models import (
    Bill, BillStatus, PersonLineItemClaim, Settlement, Tab, TabGroup, TabGroupMember, TabPerson,
)
from ninjatab.tabs.schemas import (
    ClaimGroupInviteSchema, CursorPageSchema, GroupCreateSchema, GroupDetailSchema,
    GroupInviteInfoSchema, GroupListSchema, GroupMemberCreateSchema, TabListSchema,
)
from ninjatab.utilities.analytics import safe_capture

from .common import _annotate_tab_list, _apply_tab_cursor, _create_period_tab, _sync_contacts_for_tab

User = get_user_model()

group_router = Router(tags=["groups"], auth=JWTBearer())


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
    """List houses. Pass ?archived=true to list archived houses instead.

    Each house carries its current open period's id and *live* spend (not the
    cumulative house total), so the list can show "£x this period" and have it
    reset to zero the moment a period is settled and a fresh one opens.
    """
    qs = (
        TabGroup.objects.accessible_by(request.auth)
        .filter(is_archived=archived)
        .annotate(
            member_count=Count('members', distinct=True),
            period_count=Count('tabs', distinct=True),
        )
        .prefetch_related('tabs__bills__line_items')
    )
    items, next_cursor = _apply_tab_cursor(qs, cursor)

    for group in items:
        current = next(
            (
                t for t in sorted(group.tabs.all(), key=lambda t: t.created_at, reverse=True)
                if not t.is_settled and not t.is_archived
            ),
            None,
        )
        spend, _ok = _tab_settlement_total(current, group.settlement_currency) if current else (0, True)
        group.current_period_id = str(current.uuid) if current else None
        group.current_period_spend = spend
        group.current_period_spend_display = minor_to_decimal(spend, group.settlement_currency)

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


@group_router.post("/{group_id}/leave")
@transaction.atomic
def leave_group(request, group_id: str):
    """Leave a house: fully unlink the requesting user from it.

    Nulls their roster ``TabGroupMember.user`` and their ``TabPerson.user`` on
    every period (current + settled), so they lose all access to the house and
    its history. The person rows stay behind as unlinked placeholders, keeping
    each period's spend record intact. If the leaver created the house, the
    owner link (``created_by``) is cleared too — there are no owner-only
    actions, and a null ``created_by`` is already a supported state — so they
    don't retain access through it. They (or anyone) can be re-invited later as
    long as someone is still on the house.
    """
    group = get_object_or_404(TabGroup.objects.accessible_by(request.auth), uuid=group_id)
    user = request.auth

    is_member = group.members.filter(user=user).exists()
    is_creator = group.created_by_id == user.id
    if not is_member and not is_creator:
        raise HttpError(400, "You are not a member of this house")

    group.members.filter(user=user).update(user=None)
    TabPerson.objects.filter(tab__group=group, user=user).update(user=None)

    if is_creator:
        Tab.objects.filter(group=group, created_by=user).update(created_by=None)
        group.created_by = None
        group.save(update_fields=["created_by", "updated_at"])

    safe_capture(user.uuid, "group_left", properties={"group_id": str(group.uuid)})
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
