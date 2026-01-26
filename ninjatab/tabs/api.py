from ninja import Router
from ninja.errors import HttpError
from django.shortcuts import get_object_or_404
from django.db import transaction

from ninjatab.tabs.models import *
from ninjatab.tabs.schemas import *
from ninjatab.tabs.simp import simp_tab
from ninjatab.tabs.exchange import ExchangeRateNotFoundError

tab_router = Router(tags=["tabs"])
bill_router = Router(tags=["bills"])


@tab_router.post("/", response=TabSchema)
@transaction.atomic
def create_tab(request, payload: TabCreateSchema):
    """Create a new tab with people"""
    tab = Tab.objects.create(
        name=payload.name,
        description=payload.description,
        default_currency=payload.default_currency
    )

    for person_data in payload.people:
        TabPerson.objects.create(
            tab=tab,
            name=person_data.name,
            email=person_data.email
        )

    # Refresh to get related people
    tab.refresh_from_db()
    tab = Tab.objects.prefetch_related(
        'people__user',
        'settlements__from_person__user',
        'settlements__to_person__user'
    ).get(id=tab.id)

    return tab


@tab_router.get("/", response=List[TabListSchema])
def list_tabs(request):
    """List all tabs"""
    tabs = Tab.objects.all()
    return tabs


@tab_router.get("/{tab_id}", response=TabSchema)
def retrieve_tab(request, tab_id: int):
    """Retrieve a tab with all its people"""
    tab = get_object_or_404(
        Tab.objects.prefetch_related(
            'people__user',
            'settlements__from_person__user',
            'settlements__to_person__user'
        ),
        id=tab_id
    )
    return tab


@tab_router.patch("/{tab_id}", response=TabSchema)
@transaction.atomic
def update_tab(request, tab_id: int, payload: TabUpdateSchema):
    """Update tab fields (settlement_currency)"""
    tab = get_object_or_404(
        Tab.objects.prefetch_related(
            'people__user',
            'settlements__from_person__user',
            'settlements__to_person__user'
        ),
        id=tab_id
    )

    # Update fields if provided
    if payload.settlement_currency is not None:
        tab.settlement_currency = payload.settlement_currency

    tab.save()

    # Refresh to get updated data
    tab.refresh_from_db()

    return tab


@tab_router.delete("/{tab_id}")
def delete_tab(request, tab_id: int):
    """Delete a tab"""
    tab = get_object_or_404(Tab, id=tab_id)
    tab.delete()
    return {"success": True}


@tab_router.post("/{tab_id}/close", response=TabSchema)
@transaction.atomic
def close_tab(request, tab_id: int):
    """Close a tab (prevents adding new bills or splits) and close all bills"""
    tab = get_object_or_404(
        Tab.objects.prefetch_related(
            'people__user',
            'settlements__from_person__user',
            'settlements__to_person__user'
        ),
        id=tab_id
    )
    tab.is_settled = True
    tab.save()

    # Close all bills in this tab and their line items
    bills = tab.bills.all()
    for bill in bills:
        bill.is_closed = True
        bill.save()
        # Close all line items in each bill
        bill.line_items.all().update(is_closed=True)

    # Refresh to get updated data
    tab.refresh_from_db()
    tab = Tab.objects.prefetch_related(
        'people__user',
        'settlements__from_person__user',
        'settlements__to_person__user'
    ).get(id=tab.id)

    return tab


@tab_router.post("/{tab_id}/simplify", response=SimplifyResultSchema)
@transaction.atomic
def simplify_tab(request, tab_id: int):
    """
    Calculate and save simplified settlements for a tab.
    Settlements are calculated in the tab's settlement_currency, converting bills as needed.
    Tab remains open and settlements can be regenerated.
    """
    tab = get_object_or_404(
        Tab.objects.prefetch_related('people__user', 'bills__line_items__person_claims__person'),
        id=tab_id
    )

    # Check if there are any non-archived bills
    bills = tab.bills.exclude(status=BillStatus.ARCHIVED)
    if not bills.exists():
        raise HttpError(400, "Tab has no bills to simplify")

    # Use tab's settlement_currency for settlements
    settlement_currency = tab.settlement_currency

    # Delete existing settlements for this tab (replace existing behavior)
    Settlement.objects.filter(tab=tab).delete()

    try:
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


@tab_router.get("/{tab_id}/person-totals", response=List[PersonSpendingTotalSchema])
def get_tab_person_totals(request, tab_id: int):
    """Get total spending per person for a tab in settlement currency (with currency conversion)"""
    from .exchange import convert_amount, ExchangeRateNotFoundError

    tab = get_object_or_404(
        Tab.objects.prefetch_related('bills__line_items__person_claims__person'),
        id=tab_id
    )

    settlement_currency = tab.settlement_currency  # Use tab's settlement currency
    person_totals = {}

    # Sum up all person claims across all bills, converting to settlement currency
    for bill in tab.bills.all():
        bill_currency = bill.currency
        for line_item in bill.line_items.all():
            for claim in line_item.person_claims.all():
                person_id = claim.person.id
                if person_id not in person_totals:
                    person_totals[person_id] = {
                        'person_id': person_id,
                        'person_name': claim.person.name,
                        'total': Decimal('0')
                    }

                amount = claim.calculated_amount or Decimal('0')

                # Convert to GBP if needed
                if bill_currency != settlement_currency:
                    try:
                        amount = convert_amount(amount, bill_currency, settlement_currency)
                    except ExchangeRateNotFoundError:
                        # If conversion fails, skip this claim
                        continue

                person_totals[person_id]['total'] += amount

    return list(person_totals.values())


# Bill Endpoints
@bill_router.post("/", response=BillSchema)
@transaction.atomic
def create_bill(request, payload: BillCreateSchema):
    """Create a new bill with line items"""
    tab = get_object_or_404(Tab, id=payload.tab_id)
    creator = get_object_or_404(TabPerson, id=payload.creator_id, tab=tab)

    paid_by = None
    if payload.paid_by_id:
        paid_by = get_object_or_404(TabPerson, id=payload.paid_by_id, tab=tab)

    bill = Bill.objects.create(
        tab=tab,
        description=payload.description,
        currency=payload.currency,
        creator=creator,
        paid_by=paid_by,
        date=payload.date if payload.date else date.today()
    )

    # Create line items with splits
    for line_item_data in payload.line_items:
        line_item = LineItem.objects.create(
            bill=bill,
            description=line_item_data.description,
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
        person = get_object_or_404(TabPerson, id=person_split.person_id, tab=tab)

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

        PersonLineItemClaim.objects.create(
            person=person,
            line_item=line_item,
            split_value=person_split.split_value,
            calculated_amount=calculated_amount
        )


@bill_router.post("/{bill_id}/submit-splits", response=BillSchema)
@transaction.atomic
def submit_bill_splits(request, bill_id: int, payload: BillSplitSubmitSchema):
    """Submit or update splits for a bill from the UI"""
    bill = get_object_or_404(
        Bill.objects.prefetch_related('line_items', 'tab__people'),
        id=bill_id
    )

    if bill.id != payload.bill_id:
        return {"error": "Bill ID mismatch"}, 400

    # Process each line item split
    for line_item_split in payload.line_item_splits:
        line_item = get_object_or_404(
            LineItem,
            id=line_item_split.line_item_id,
            bill=bill
        )

        # Delete existing claims for this line item
        PersonLineItemClaim.objects.filter(line_item=line_item).delete()

        # Create new claims
        _create_person_claims(line_item, line_item_split.person_splits, bill.tab)

    # Refresh the bill to get updated data
    bill.refresh_from_db()
    return bill


@bill_router.get("/", response=List[BillListSchema])
def list_bills(request, tab_id: int = None):
    """List all bills, optionally filtered by tab"""
    bills = Bill.objects.all()
    if tab_id:
        bills = bills.filter(tab_id=tab_id)
    bills = bills.select_related('paid_by__user').prefetch_related('line_items')
    return bills


@bill_router.get("/{bill_id}", response=BillSchema)
def retrieve_bill(request, bill_id: int):
    """Retrieve a bill with all its line items and claims"""
    bill = get_object_or_404(
        Bill.objects.prefetch_related(
            'line_items__person_claims__person__user',
            'creator__user',
            'paid_by__user'
        ),
        id=bill_id
    )
    return bill


@bill_router.patch("/{bill_id}", response=BillSchema)
@transaction.atomic
def update_bill(request, bill_id: int, payload: BillUpdateSchema):
    """Update bill fields (description, currency, paid_by)"""
    bill = get_object_or_404(
        Bill.objects.prefetch_related(
            'line_items__person_claims__person__user',
            'creator__user',
            'paid_by__user'
        ),
        id=bill_id
    )

    # Update fields if provided
    if payload.description is not None:
        bill.description = payload.description

    if payload.currency is not None:
        bill.currency = payload.currency

    if payload.paid_by_id is not None:
        paid_by = get_object_or_404(TabPerson, id=payload.paid_by_id, tab=bill.tab)
        bill.paid_by = paid_by

    bill.save()

    # Refresh to get updated data
    bill.refresh_from_db()

    return bill


@bill_router.post("/{bill_id}/close", response=BillSchema)
@transaction.atomic
def close_bill(request, bill_id: int):
    """Close a bill and all its line items"""
    bill = get_object_or_404(
        Bill.objects.prefetch_related(
            'line_items__person_claims__person__user',
            'creator__user',
            'paid_by__user'
        ),
        id=bill_id
    )

    # Close the bill
    bill.is_closed = True
    bill.save()

    # Close all line items
    bill.line_items.all().update(is_closed=True)

    # Refresh to get updated data
    bill.refresh_from_db()

    return bill


@bill_router.delete("/{bill_id}")
def delete_bill(request, bill_id: int):
    """Delete a bill"""
    bill = get_object_or_404(Bill, id=bill_id)
    bill.delete()
    return {"success": True}

