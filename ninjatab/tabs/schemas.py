from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional, List, Any
from datetime import datetime, date
from decimal import Decimal
from enum import Enum


class CurrencyEnum(str, Enum):
    USD = 'USD'
    EUR = 'EUR'
    GBP = 'GBP'
    JPY = 'JPY'
    CAD = 'CAD'
    TRY = 'TRY'


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
    id: int
    username: str
    email: EmailStr
    first_name: str
    last_name: str

    class Config:
        from_attributes = True


class TabPersonSchema(BaseModel):
    id: int
    name: str
    email: Optional[EmailStr] = None
    user: Optional[UserSchema] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TabPersonCreateSchema(BaseModel):
    name: str
    email: Optional[EmailStr] = None


class PersonLineItemClaimSchema(BaseModel):
    id: int
    person_id: int
    person_name: str
    split_value: Optional[Decimal] = None
    calculated_amount: Optional[Decimal] = None
    has_claimed: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def extract_person_data(cls, data: Any) -> Any:
        # If data is a Django model instance, extract person_id and person_name
        if hasattr(data, 'person'):
            return {
                'id': data.id,
                'person_id': data.person.id,
                'person_name': data.person.name,
                'split_value': data.split_value,
                'calculated_amount': data.calculated_amount,
                'has_claimed': data.has_claimed,
                'created_at': data.created_at,
                'updated_at': data.updated_at,
            }
        return data


class LineItemSchema(BaseModel):
    id: int
    description: str
    value: Decimal
    split_type: SplitTypeEnum
    is_closed: bool
    person_claims: List[PersonLineItemClaimSchema]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def extract_person_claims(cls, data: Any) -> Any:
        # If data is a Django model instance, convert person_claims relationship to list
        if hasattr(data, 'person_claims'):
            if hasattr(data.person_claims, 'all'):
                # It's a related manager, evaluate it
                person_claims_list = list(data.person_claims.all())
                # Create a dict with all fields
                return {
                    'id': data.id,
                    'description': data.description,
                    'value': data.value,
                    'split_type': data.split_type,
                    'is_closed': data.is_closed,
                    'person_claims': person_claims_list,
                    'created_at': data.created_at,
                    'updated_at': data.updated_at,
                }
        return data


class PersonSplitCreateSchema(BaseModel):
    person_id: int
    split_value: Optional[Decimal] = None


class LineItemCreateSchema(BaseModel):
    description: str
    value: Decimal
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
            total_assigned = sum(ps.split_value for ps in v)
            if total_assigned > line_item_value:
                raise ValueError(
                    f"Total split value ({total_assigned}) exceeds line item value ({line_item_value})"
                )

        return v


class BillSchema(BaseModel):
    id: int
    description: str
    currency: CurrencyEnum
    status: BillStatusEnum
    creator: TabPersonSchema
    paid_by: Optional[TabPersonSchema] = None
    date: date
    is_closed: bool
    line_items: List[LineItemSchema]
    total_amount: Decimal
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def extract_line_items(cls, data: Any) -> Any:
        # If data is a Django model instance, convert line_items relationship to list
        if hasattr(data, 'line_items'):
            if hasattr(data.line_items, 'all'):
                # It's a related manager, evaluate it
                line_items_list = list(data.line_items.all())
                # Create a dict with all fields
                return {
                    'id': data.id,
                    'description': data.description,
                    'currency': data.currency,
                    'status': data.status,
                    'creator': data.creator,
                    'paid_by': data.paid_by,
                    'date': data.date,
                    'is_closed': data.is_closed,
                    'line_items': line_items_list,
                    'total_amount': data.total_amount,
                    'created_at': data.created_at,
                    'updated_at': data.updated_at,
                }
        return data


class BillCreateSchema(BaseModel):
    tab_id: int
    description: str
    currency: CurrencyEnum
    creator_id: int
    paid_by_id: Optional[int] = None
    date: Optional[date] = None
    line_items: List[LineItemCreateSchema] = []


class BillSplitSubmitSchema(BaseModel):
    """Schema for submitting splits from the UI"""
    bill_id: int
    split_mode: SplitModeEnum
    line_item_splits: List['LineItemSplitSubmitSchema']


class LineItemSplitSubmitSchema(BaseModel):
    line_item_id: int
    person_splits: List['PersonSplitSubmitSchema']


class PersonSplitSubmitSchema(BaseModel):
    person_id: int
    split_value: Optional[Decimal] = None


class BillListSchema(BaseModel):
    id: int
    description: str
    currency: CurrencyEnum
    status: BillStatusEnum
    date: date
    is_closed: bool
    total_amount: Decimal
    created_at: datetime

    class Config:
        from_attributes = True


class TabSchema(BaseModel):
    id: int
    name: str
    description: str
    default_currency: CurrencyEnum
    is_settled: bool
    bill_count: int
    people: List[TabPersonSchema]
    settlements: List['SettlementSchema']
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def extract_people(cls, data: Any) -> Any:
        # If data is a Django model instance, convert people relationship to list
        if hasattr(data, 'people'):
            if hasattr(data.people, 'all'):
                # It's a related manager, evaluate it
                people_list = list(data.people.all())
                settlements_list = list(data.settlements.all()) if hasattr(data, 'settlements') else []
                # Create a dict with all fields
                return {
                    'id': data.id,
                    'name': data.name,
                    'description': data.description,
                    'default_currency': data.default_currency,
                    'is_settled': data.is_settled,
                    'bill_count': data.bill_count,
                    'people': people_list,
                    'settlements': settlements_list,
                    'created_at': data.created_at,
                    'updated_at': data.updated_at,
                }
        return data


class TabListSchema(BaseModel):
    id: int
    name: str
    description: str
    default_currency: CurrencyEnum
    is_settled: bool
    bill_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TabCreateSchema(BaseModel):
    name: str
    description: str = ""
    default_currency: CurrencyEnum = CurrencyEnum.GBP
    people: List[TabPersonCreateSchema] = Field(min_length=1)


class SettlementSchema(BaseModel):
    id: int
    from_person: TabPersonSchema
    to_person: TabPersonSchema
    amount: Decimal
    currency: CurrencyEnum
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SimplifyResultSchema(BaseModel):
    settlements: List[SettlementSchema]
    message: str