from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from typing import Optional, List, Any, Generic, TypeVar
from datetime import datetime, date as Date
from decimal import Decimal
from enum import Enum

from ninjatab.currencies.currency_utils import minor_to_decimal
from ninjatab.currencies.exchange import convert_amount, ExchangeRateNotFoundError

T = TypeVar('T')


class CursorPageSchema(BaseModel, Generic[T]):
    items: List[T]
    next_cursor: Optional[str] = None


class CurrencyEnum(str, Enum):
    USD = 'USD'
    EUR = 'EUR'
    GBP = 'GBP'
    JPY = 'JPY'
    CAD = 'CAD'
    TRY = 'TRY'
    PLN = 'PLN'
    CZK = 'CZK'
    AUD = 'AUD'
    CHF = 'CHF'
    HUF = 'HUF'
    BGN = 'BGN'
    MXN = 'MXN'
    TBH = 'THB'


class SplitTypeEnum(str, Enum):
    SHARES = 'shares'
    VALUE = 'value'


class BillStatusEnum(str, Enum):
    OPEN = 'open'
    ALL_CLAIMED = 'all_claimed'
    ALL_PAID = 'all_paid'
    ARCHIVED = 'archived'


class SplitModeEnum(str, Enum):
    EVEN = 'even'
    UNEVEN = 'uneven'


# Schemas
class UserSchema(BaseModel):
    id: str
    username: str
    email: EmailStr
    first_name: str
    last_name: str

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_uuid(cls, data: Any) -> Any:
        if hasattr(data, 'uuid'):
            return {
                'id': str(data.uuid),
                'username': data.username,
                'email': data.email.lower(),
                'first_name': data.first_name,
                'last_name': data.last_name,
            }
        return data


class TabPersonSchema(BaseModel):
    id: str
    name: str
    user: Optional[UserSchema] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_uuid(cls, data: Any) -> Any:
        if hasattr(data, 'uuid'):
            return {
                'id': str(data.uuid),
                'name': data.name,
                'user': data.user,
                'created_at': data.created_at,
                'updated_at': data.updated_at,
            }
        return data


class TabPersonCreateSchema(BaseModel):
    name: str
    email: Optional[str] = None
    user_id: Optional[str] = None


class PersonLineItemClaimSchema(BaseModel):
    id: str
    person_id: str
    person_name: str
    split_value: Optional[int] = None
    split_value_display: Optional[Decimal] = None
    calculated_amount: Optional[int] = None
    calculated_amount_display: Optional[Decimal] = None
    settlement_amount: Optional[int] = None
    has_claimed: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_person_data(cls, data: Any) -> Any:
        if hasattr(data, 'person'):
            return {
                'id': str(data.uuid),
                'person_id': str(data.person.uuid),
                'person_name': data.person.name,
                'split_value': data.split_value,
                'split_value_display': None,  # computed below if currency provided
                'calculated_amount': data.calculated_amount,
                'calculated_amount_display': None,  # computed below if currency provided
                'settlement_amount': data.settlement_amount,
                'has_claimed': data.has_claimed,
                'created_at': data.created_at,
                'updated_at': data.updated_at,
            }
        if isinstance(data, dict):
            currency = data.get('currency')
            split_type = data.get('split_type')
            if currency:
                # Only show split_value_display when it's a currency amount (VALUE mode)
                if split_type == 'value' and data.get('split_value') is not None:
                    data['split_value_display'] = minor_to_decimal(data['split_value'], currency)
                if data.get('calculated_amount') is not None:
                    data['calculated_amount_display'] = minor_to_decimal(data['calculated_amount'], currency)
        return data


class LineItemSchema(BaseModel):
    id: str
    description: str
    translated_name: str = ''
    value: int
    value_display: Optional[Decimal] = None
    split_type: SplitTypeEnum
    person_claims: List[PersonLineItemClaimSchema]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_person_claims(cls, data: Any) -> Any:
        if hasattr(data, 'person_claims'):
            if hasattr(data.person_claims, 'all'):
                person_claims_list = list(data.person_claims.all())
                return {
                    'id': str(data.uuid),
                    'description': data.description,
                    'translated_name': data.translated_name,
                    'value': data.value,
                    'value_display': None,  # set by BillSchema when currency is available
                    'split_type': data.split_type,
                    'person_claims': person_claims_list,
                    'created_at': data.created_at,
                    'updated_at': data.updated_at,
                }
        if isinstance(data, dict):
            currency = data.get('currency')
            split_type = data.get('split_type')
            if currency and data.get('value') is not None:
                data['value_display'] = minor_to_decimal(data['value'], currency)
            # Enrich each claim dict with currency and split_type for display computation
            if currency and 'person_claims' in data:
                enriched_claims = []
                for claim in data['person_claims']:
                    if hasattr(claim, 'person'):
                        # Django model instance — build dict with currency context
                        enriched_claims.append({
                            'id': str(claim.uuid),
                            'person_id': str(claim.person.uuid),
                            'person_name': claim.person.name,
                            'split_value': claim.split_value,
                            'calculated_amount': claim.calculated_amount,
                            'has_claimed': claim.has_claimed,
                            'created_at': claim.created_at,
                            'updated_at': claim.updated_at,
                            'currency': currency,
                            'split_type': split_type,
                        })
                    elif isinstance(claim, dict):
                        claim = dict(claim)
                        claim.setdefault('currency', currency)
                        claim.setdefault('split_type', split_type)
                        enriched_claims.append(claim)
                    else:
                        enriched_claims.append(claim)
                data['person_claims'] = enriched_claims
        return data


class PersonSplitCreateSchema(BaseModel):
    person_id: str
    split_value: Optional[int] = None


class LineItemCreateSchema(BaseModel):
    description: str
    translated_name: str = ''
    value: int
    split_type: SplitTypeEnum = SplitTypeEnum.SHARES
    person_splits: List[PersonSplitCreateSchema] = []

    @field_validator('person_splits')
    @classmethod
    def validate_person_splits(cls, v, info):
        if not v:
            return v

        split_type = info.data.get('split_type')
        line_item_value = info.data.get('value')

        if split_type == SplitTypeEnum.VALUE:
            total_assigned = sum(ps.split_value for ps in v if ps.split_value)
            if total_assigned > line_item_value:
                raise ValueError(
                    f"Total split value ({total_assigned}) exceeds line item value ({line_item_value})"
                )

        return v


def _receipt_image_url(bill) -> str:
    """Return a presigned URL for the bill's receipt image, or fall back to the legacy URL."""
    key = getattr(bill, 'receipt_image_key', '') or ''
    if key:
        from ninjatab.tabs.receipt_service import generate_presigned_url
        return generate_presigned_url(key)
    return getattr(bill, 'receipt_image_url', '') or ''


class BillSchema(BaseModel):
    id: str
    description: str
    currency: CurrencyEnum
    status: BillStatusEnum
    creator: TabPersonSchema
    paid_by: Optional[TabPersonSchema] = None
    date: Date
    line_items: List[LineItemSchema]
    total_amount: int
    total_amount_display: Optional[Decimal] = None
    settlement_total: Optional[int] = None
    receipt_image_url: str = ''
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_line_items(cls, data: Any) -> Any:
        if hasattr(data, 'line_items'):
            if hasattr(data.line_items, 'all'):
                currency = data.currency
                line_items_list = [
                    {
                        'id': str(li.uuid),
                        'description': li.description,
                        'translated_name': li.translated_name,
                        'value': li.value,
                        'split_type': li.split_type,
                        'person_claims': list(li.person_claims.all()) if hasattr(li.person_claims, 'all') else [],
                        'created_at': li.created_at,
                        'updated_at': li.updated_at,
                        'currency': currency,
                    }
                    for li in data.line_items.all()
                ]
                total_amount = data.total_amount
                settlement_currency = getattr(getattr(data, 'tab', None), 'settlement_currency', None)
                settlement_total = None
                if settlement_currency:
                    try:
                        settlement_total = convert_amount(total_amount, currency, settlement_currency)
                    except ExchangeRateNotFoundError:
                        pass
                return {
                    'id': str(data.uuid),
                    'description': data.description,
                    'currency': currency,
                    'status': data.status,
                    'creator': data.creator,
                    'paid_by': data.paid_by,
                    'date': data.date,
                    'line_items': line_items_list,
                    'total_amount': total_amount,
                    'total_amount_display': minor_to_decimal(total_amount, currency),
                    'settlement_total': settlement_total,
                    'receipt_image_url': _receipt_image_url(data),
                    'created_at': data.created_at,
                    'updated_at': data.updated_at,
                }
        return data


class BillCreateSchema(BaseModel):
    tab_id: str
    description: str
    currency: CurrencyEnum
    paid_by_id: Optional[str] = None
    date: Optional[Date] = None
    receipt_image_key: str = ''
    line_items: List[LineItemCreateSchema] = []


class BillUpdateSchema(BaseModel):
    """Schema for updating bill fields"""
    description: Optional[str] = None
    currency: Optional[CurrencyEnum] = None
    paid_by_id: Optional[str] = None
    date: Optional[Date] = None


class BillSplitSubmitSchema(BaseModel):
    """Schema for submitting splits from the UI"""
    bill_id: str
    split_mode: SplitModeEnum
    line_item_splits: List['LineItemSplitSubmitSchema']


class LineItemSplitSubmitSchema(BaseModel):
    line_item_id: str
    person_splits: List['PersonSplitSubmitSchema']


class PersonSplitSubmitSchema(BaseModel):
    person_id: str
    split_value: Optional[int] = None


class BillListSchema(BaseModel):
    id: str
    description: str
    currency: CurrencyEnum
    status: BillStatusEnum
    date: Date
    total_amount: int
    total_amount_display: Optional[Decimal] = None
    settlement_total: Optional[int] = None
    paid_by: Optional[TabPersonSchema] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_uuid(cls, data: Any) -> Any:
        if hasattr(data, 'uuid'):
            currency = data.currency
            total_amount = data.total_amount
            settlement_currency = getattr(getattr(data, 'tab', None), 'settlement_currency', None)
            settlement_total = None
            if settlement_currency and settlement_currency != currency:
                try:
                    settlement_total = convert_amount(total_amount, currency, settlement_currency)
                except ExchangeRateNotFoundError:
                    pass
            return {
                'id': str(data.uuid),
                'description': data.description,
                'currency': currency,
                'status': data.status,
                'date': data.date,
                'total_amount': total_amount,
                'total_amount_display': minor_to_decimal(total_amount, currency),
                'settlement_total': settlement_total,
                'paid_by': data.paid_by,
                'created_at': data.created_at,
            }
        return data


class TabSchema(BaseModel):
    id: str
    name: str
    description: str
    default_currency: CurrencyEnum
    settlement_currency: CurrencyEnum
    is_settled: bool
    is_archived: bool
    is_pro: bool
    is_demo: bool
    invite_code: Optional[str] = None
    bill_count: int
    people: List[TabPersonSchema]
    settlements: List['SettlementSchema']
    settlement_currency_settled_total: Optional[int] = None
    settlement_currency_settled_total_display: Optional[Decimal] = None
    totals_by_currency: dict[str, int] = {}
    totals_by_currency_display: dict[str, Decimal] = {}
    group_spend: Optional[int] = None
    group_spend_display: Optional[Decimal] = None
    user_owes: int = 0
    user_owes_display: Decimal = Decimal('0')
    user_owed: int = 0
    user_owed_display: Decimal = Decimal('0')
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_people(cls, data: Any) -> Any:
        if hasattr(data, 'people'):
            if hasattr(data.people, 'all'):
                from ninjatab.currencies.exchange import convert_amount

                people_list = list(data.people.all())
                settlements_list = list(data.settlements.all()) if hasattr(data, 'settlements') else []

                settlement_currency = data.settlement_currency
                totals_by_currency = {}
                group_spend = 0
                conversion_ok = True
                bills = [b for b in data.bills.all() if b.status != 'archived']
                for bill in bills:
                    bill_total = sum((li.value or 0) for li in bill.line_items.all())
                    totals_by_currency[bill.currency] = (
                        totals_by_currency.get(bill.currency, 0) + bill_total
                    )
                    if conversion_ok:
                        try:
                            if bill.currency != settlement_currency:
                                bill_total = convert_amount(bill_total, bill.currency, settlement_currency)
                            group_spend += bill_total
                        except Exception:
                            conversion_ok = False

                totals_by_currency_display = {
                    currency: minor_to_decimal(amount, currency)
                    for currency, amount in totals_by_currency.items()
                }
                group_spend_val = group_spend if conversion_ok else None
                group_spend_display = minor_to_decimal(group_spend_val, settlement_currency) if conversion_ok else None

                user_owes = getattr(data, 'user_owes', 0) or 0
                user_owed = getattr(data, 'user_owed', 0) or 0

                settled_total = data.settlement_currency_settled_total
                return {
                    'id': str(data.uuid),
                    'name': data.name,
                    'description': data.description,
                    'default_currency': data.default_currency,
                    'settlement_currency': settlement_currency,
                    'is_settled': data.is_settled,
                    'is_archived': data.is_archived,
                    'is_pro': data.is_pro,
                    'is_demo': data.is_demo,
                    'invite_code': str(data.invite_code) if data.invite_code else None,
                    'bill_count': len(list(data.bills.all())),
                    'people': people_list,
                    'settlements': settlements_list,
                    'settlement_currency_settled_total': settled_total,
                    'settlement_currency_settled_total_display': minor_to_decimal(settled_total, settlement_currency),
                    'totals_by_currency': totals_by_currency,
                    'totals_by_currency_display': totals_by_currency_display,
                    'group_spend': group_spend_val,
                    'group_spend_display': group_spend_display,
                    'user_owes': user_owes,
                    'user_owes_display': minor_to_decimal(user_owes, settlement_currency) or Decimal('0'),
                    'user_owed': user_owed,
                    'user_owed_display': minor_to_decimal(user_owed, settlement_currency) or Decimal('0'),
                    'created_at': data.created_at,
                    'updated_at': data.updated_at,
                }
        return data


class TabListSchema(BaseModel):

    id: UUID = Field(validation_alias="uuid")
    name: str
    description: str
    default_currency: CurrencyEnum
    is_settled: bool
    is_archived: bool
    is_pro: bool
    is_demo: bool
    bill_count: int
    people_count: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TabCreateSchema(BaseModel):
    name: str
    description: str = ""
    default_currency: CurrencyEnum = CurrencyEnum.GBP
    settlement_currency: CurrencyEnum = CurrencyEnum.GBP
    people: List[TabPersonCreateSchema] = Field(min_length=1)


class TabUpdateSchema(BaseModel):
    settlement_currency: CurrencyEnum = None


class SettlementSchema(BaseModel):
    id: str
    from_person: TabPersonSchema
    to_person: TabPersonSchema
    amount: int
    amount_display: Optional[Decimal] = None
    currency: CurrencyEnum
    paid: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_persons(cls, data: Any) -> Any:
        if hasattr(data, 'from_person') and hasattr(data, 'to_person'):
            amount = data.amount
            currency = data.currency
            return {
                'id': str(data.uuid),
                'from_person': data.from_person,
                'to_person': data.to_person,
                'amount': amount,
                'amount_display': minor_to_decimal(amount, currency),
                'currency': currency,
                'paid': data.paid,
                'created_at': data.created_at,
                'updated_at': data.updated_at,
            }
        return data


class SimplifyResultSchema(BaseModel):
    settlements: List[SettlementSchema]
    message: str


class PersonSpendingTotalSchema(BaseModel):
    person_id: str
    person_name: str
    total: int
    total_display: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def compute_display(cls, data: Any) -> Any:
        if isinstance(data, dict) and 'currency' in data:
            data = dict(data)
            data['total_display'] = minor_to_decimal(data.get('total'), data['currency'])
        return data


class InvitePersonSchema(BaseModel):
    id: str
    name: str

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_uuid(cls, data: Any) -> Any:
        if hasattr(data, 'uuid'):
            return {
                'id': str(data.uuid),
                'name': data.name,
            }
        return data


class InviteTabInfoSchema(BaseModel):
    tab_id: str
    tab_name: str
    people: List[InvitePersonSchema]
    user_already_on_tab: bool = False


class ContactSchema(BaseModel):
    id: str
    user_id: str
    first_name: str
    last_name: str
    email: str

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_contact(cls, data: Any) -> Any:
        if hasattr(data, 'contact_user'):
            return {
                'id': str(data.uuid),
                'user_id': str(data.contact_user.uuid),
                'first_name': data.contact_user.first_name,
                'last_name': data.contact_user.last_name,
                'email': data.contact_user.email.lower(),
            }
        return data


class ClaimInviteSchema(BaseModel):
    person_id: str
    email: EmailStr
