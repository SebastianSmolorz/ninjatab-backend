from typing import Literal, Optional

from pydantic import BaseModel


# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server. `kind` / bool flags are
# fixed-token decodes, so that concern does not apply to them.
class _ItemAdjustment(BaseModel):
    name: str
    translated_name: str
    # Signed delta (negative for reductions) OR the replacement price when
    # is_new_price is true ("3 for £3.00", "cc price £5.50").
    amount: str
    is_new_price: bool


class _Item(BaseModel):
    name: str
    translated_name: str
    # The printed price of the item's own priced row (gross). The server folds
    # adjustments into a net total in post-processing.
    total: str
    quantity: Optional[int] = None
    price_per_quantity: Optional[str] = None
    adjustments: Optional[list[_ItemAdjustment]] = None
    # Raw OCR text of the printed receipt row(s) this item came from. Used for
    # debugging and line-item alignment in validation; ignored by splitting.
    receipt_line_text: Optional[str] = None


class _Charge(BaseModel):
    name: str
    translated_name: str
    # "tip" for tip/gratuity; "charge" for everything else (tax, VAT, fees,
    # service charges, surcharges; discounts carry a negative amount).
    kind: Literal["tip", "charge"]
    amount: str
    # True when the charge is already contained in the item prices (e.g.
    # EU-style "VAT included"); False when it adds on top (US sales tax, tips).
    included_in_item_totals: bool


class _Document(BaseModel):
    receipt_language: str
    receipt_language_code: Optional[str] = None
    items: list[_Item]
    # The printed subtotal only — never computed by the model.
    subtotal: Optional[str] = None
    receipt_total: Optional[str] = None
    items_total: Optional[str] = None
    charges: Optional[list[_Charge]] = None
    receipt_establishment_name: Optional[str] = None
    currency_code: Optional[str] = None
    datetime_of_receipt: Optional[str] = None
