from typing import Optional

from pydantic import BaseModel


# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server.
class _Item(BaseModel):
    name: str
    translated_name: str
    total: str
    quantity: Optional[int] = None
    price_per_quantity: Optional[str] = None
    # A loyalty/clubcard-style saving printed against this specific item, as a
    # negative decimal string (e.g. "-1.00"). The model transcribes the printed
    # saving here and leaves `total` as the pre-discount price; the server does
    # the arithmetic (see `_apply_item_discounts`).
    discount: Optional[str] = None
    # Raw OCR text of the printed receipt row(s) this item came from. Used for
    # debugging and line-item alignment in validation; ignored by splitting.
    receipt_line_text: Optional[str] = None


class _OtherCharge(BaseModel):
    name: str
    translated_name: str
    amount: str


class _Document(BaseModel):
    receipt_language: str
    receipt_language_code: Optional[str] = None
    items: list[_Item]
    receipt_total: Optional[str] = None
    items_total: Optional[str] = None
    receipt_establishment_name: Optional[str] = None
    currency_code: Optional[str] = None
    datetime_of_receipt: Optional[str] = None
    tax: Optional[str] = None
    tip: Optional[str] = None
    service_charge: Optional[str] = None
    other_charges: Optional[list[_OtherCharge]] = None
