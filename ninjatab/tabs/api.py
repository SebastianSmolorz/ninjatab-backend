from ninja import Router
from django.shortcuts import get_object_or_404
from django.db import transaction

from ninjatab.tabs.models import *
from ninjatab.tabs.schemas import *

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
    tab = Tab.objects.prefetch_related('people__user').get(id=tab.id)

    return tab


@tab_router.get("/", response=List[TabListSchema])
def list_tabs(request):
    """List all tabs"""
    tabs = Tab.objects.all()
    return tabs


@tab_router.get("/{tab_id}", response=TabSchema)
def retrieve_tab(request, tab_id: int):
    """Retrieve a tab with all its people"""
    tab = get_object_or_404(Tab.objects.prefetch_related('people__user'), id=tab_id)
    return tab


@tab_router.delete("/{tab_id}")
def delete_tab(request, tab_id: int):
    """Delete a tab"""
    tab = get_object_or_404(Tab, id=tab_id)
    tab.delete()
    return {"success": True}


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
    bills = bills.prefetch_related('line_items')
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

