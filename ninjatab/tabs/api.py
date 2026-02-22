import uuid
from datetime import datetime

import boto3
from ninja import Router, UploadedFile, File
from ninja.errors import HttpError
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.db import transaction

from ninjatab.tabs.models import *
from ninjatab.tabs.schemas import *
from ninjatab.tabs.simp import simp_tab
from ninjatab.tabs.exchange import convert_amount, ExchangeRateNotFoundError
from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.schemas import MagicLinkSuccessSchema
from ninjatab.auth.jwt_utils import create_magic_token
from ninjatab.auth.email import send_magic_link

tab_router = Router(tags=["tabs"], auth=JWTBearer())
bill_router = Router(tags=["bills"], auth=JWTBearer())


@tab_router.post("/", response=TabSchema)
@transaction.atomic
def create_tab(request, payload: TabCreateSchema):
    """Create a new tab with people"""
    tab = Tab.objects.create(
        name=payload.name,
        description=payload.description,
        default_currency=payload.default_currency,
        created_by=request.auth
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
    tabs = Tab.objects.accessible_by(request.auth)
    return tabs


@tab_router.get("/{tab_id}", response=TabSchema)
def retrieve_tab(request, tab_id: int):
    """Retrieve a tab with all its people"""
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user',
            'settlements__from_person__user',
            'settlements__to_person__user'
        ),
        id=tab_id,
    )
    return tab


@tab_router.patch("/{tab_id}", response=TabSchema)
@transaction.atomic
def update_tab(request, tab_id: int, payload: TabUpdateSchema):
    """Update tab fields (settlement_currency)"""
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user',
            'settlements__from_person__user',
            'settlements__to_person__user'
        ),
        id=tab_id,
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
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), id=tab_id)
    tab.delete()
    return {"success": True}


@tab_router.post("/{tab_id}/close", response=TabSchema)
@transaction.atomic
def close_tab(request, tab_id: int):
    """Close a tab (prevents adding new bills or splits) and close all bills"""
    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user',
            'settlements__from_person__user',
            'settlements__to_person__user'
        ),
        id=tab_id,
    )
    tab.is_settled = True
    tab.save()

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
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'people__user', 'bills__line_items__person_claims__person'
        ),
        id=tab_id,
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


@tab_router.post("/settlements/{settlement_id}/mark-paid", response=SettlementSchema)
@transaction.atomic
def mark_settlement_paid(request, settlement_id: int):
    """Mark a settlement as paid"""
    settlement = get_object_or_404(
        Settlement.objects.select_related('from_person__user', 'to_person__user'),
        id=settlement_id,
        tab__in=Tab.objects.accessible_by(request.auth)
    )
    settlement.paid = True
    settlement.save()
    return settlement


@tab_router.get("/{tab_id}/person-totals", response=List[PersonSpendingTotalSchema])
def get_tab_person_totals(request, tab_id: int):
    """Get total spending per person for a tab in settlement currency (with currency conversion)"""

    tab = get_object_or_404(
        Tab.objects.accessible_by(request.auth).prefetch_related(
            'bills__line_items__person_claims__person'
        ),
        id=tab_id,
    )

    settlement_currency = tab.settlement_currency  # Use tab's settlement currency
    person_totals = {}

    # Sum up all person claims across all non-archived bills, converting to settlement currency
    for bill in tab.bills.exclude(status=BillStatus.ARCHIVED.value):
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

                if bill_currency != settlement_currency:
                    try:
                        amount = convert_amount(amount, bill_currency, settlement_currency)
                    except ExchangeRateNotFoundError as e:
                        raise HttpError(400, f"Currency conversion failed: {str(e)}")

                person_totals[person_id]['total'] += amount

    return list(person_totals.values())


@tab_router.get("/invite/{invite_code}", response=InviteTabInfoSchema, auth=None)
def get_invite(request, invite_code: str):
    """Get tab info for invite page — no auth required"""
    tab = get_object_or_404(Tab, invite_code=invite_code)
    unclaimed = list(tab.people.filter(user__isnull=True))
    return {"tab_name": tab.name, "people": unclaimed}


@tab_router.post("/invite/{invite_code}/claim", response=MagicLinkSuccessSchema, auth=None)
def claim_invite(request, invite_code: str, payload: ClaimInviteSchema):
    """Claim a placeholder person on a tab and send a magic link — no auth required"""
    tab = get_object_or_404(Tab, invite_code=invite_code)
    person = get_object_or_404(TabPerson, id=payload.person_id, tab=tab, user__isnull=True)
    user, _ = User.objects.get_or_create(email=payload.email, defaults={"username": payload.email})
    user.first_name = person.name
    user.save(update_fields=["first_name"])
    person.user = user
    person.save()
    token = create_magic_token(user.id)
    send_magic_link(payload.email, token)
    return {"success": True}


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/octet-stream"}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


def _upload_to_spaces(file: UploadedFile, key: str) -> str:
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
    s3.upload_fileobj(
        file,
        settings.S3_BUCKET,
        key,
        ExtraArgs={"ACL": "public-read", "ContentType": file.content_type},
    )
    url = "https://tab-ninja-receipt-scans.lon1.digitaloceanspaces.com"
    return f"{url}/{key}"


@tab_router.post("/{tab_id}/upload-receipt", auth=None)
def upload_receipt(request, tab_id: int, file: UploadedFile = File(...)):
    """Upload a receipt image to storage and return its URL"""
    from pydantic import BaseModel
    from mistralai import Mistral, DocumentURLChunk, ImageURLChunk, ResponseFormat
    from mistralai.extra import response_format_from_pydantic_model

    class Item(BaseModel):
        name: str
        translated_name: str
        total: float

    class Document(BaseModel):
        receipt_language: str
        items: list[Item]
        receipt_total: float
        receipt_establishment_name: str
        currency_code: str
        # datetime_of_receipt: datetime

    get_object_or_404(Tab, id=tab_id)

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HttpError(400, f"Unsupported file type: {file.content_type}. Allowed: JPEG, PNG, WebP, HEIC")

    if file.size > MAX_UPLOAD_SIZE:
        raise HttpError(400, f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB")

    ext = file.name.rsplit(".", 1)[-1] if "." in file.name else "jpg"
    key = f"receipts/{tab_id}/{uuid.uuid4()}.{ext}"
    url = _upload_to_spaces(file, key)

    document_annotation_prompt = """
    Extract items, total, establishment name, currency code and language from this receipt. items should have these keys:
    - name - string of the item name
    - total - the float total paid for this item
    - translated_name - if the language is not English, the translated name of this item
    Currency code should be in ISO 4217 format
    Be precise.
    """
    client = Mistral(api_key=settings.MISTRAL_API_KEY)

    # Client call
    response = client.ocr.process(
        model="mistral-ocr-latest",
        pages=list(range(8)),
        document=DocumentURLChunk(
            document_url=url
        ),
        document_annotation_format=response_format_from_pydantic_model(Document),
        document_annotation_prompt=document_annotation_prompt,
        include_image_base64=True
    )

    return response


# Bill Endpoints
@bill_router.post("/", response=BillSchema)
@transaction.atomic
def create_bill(request, payload: BillCreateSchema):
    """Create a new bill with line items"""
    tab = get_object_or_404(Tab.objects.accessible_by(request.auth), id=payload.tab_id)
    creator = get_object_or_404(TabPerson, tab=tab, user=request.auth)

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
        id=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
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
    bills = Bill.objects.filter(tab__in=Tab.objects.accessible_by(request.auth))
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
        id=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
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
        id=bill_id,
        tab__in=Tab.objects.accessible_by(request.auth)
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



@bill_router.delete("/{bill_id}")
def delete_bill(request, bill_id: int):
    """Delete a bill"""
    bill = get_object_or_404(Bill, id=bill_id, tab__in=Tab.objects.accessible_by(request.auth))
    if bill.tab.is_settled:
        raise HttpError(400, "Cannot delete a bill from a closed tab")
    bill.delete()
    return {"success": True}

