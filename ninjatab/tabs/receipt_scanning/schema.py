from typing import Literal, Optional

from pydantic import BaseModel


class _Adjustment(BaseModel):
    """A discount, offer or reprice printed against a specific item.

    Modelled as belonging to its item rather than as a row of its own: a
    multibuy saving is not something anyone ordered, and leaving it as a
    free-floating negative line invites it being counted twice or dropped.
    """
    name: str
    amount: str
    # True when the line replaces the item's price outright ("was 5.00, now
    # 3.00") rather than adjusting it by a delta.
    is_new_price: bool = False


# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server.
class _Item(BaseModel):
    name: str
    translated_name: str
    total: str
    # What the row is. Asking the model to classify while it can still see the
    # row's position, alignment and typography beats re-deriving the category
    # server-side from the name alone, which is all that survives extraction.
    kind: Literal["item", "tax", "tip", "service", "discount"] = "item"
    quantity: Optional[int] = None
    price_per_quantity: Optional[str] = None
    adjustments: Optional[list[_Adjustment]] = None
    # Raw OCR text of the printed receipt row(s) this item came from. Used for
    # debugging and line-item alignment in validation; ignored by splitting.
    receipt_line_text: Optional[str] = None


class _OtherCharge(BaseModel):
    name: str
    translated_name: str
    amount: str
    # Deliberately NOT asked for: a `type` here was measured over two independent
    # 5-run captures (990 calls each) and cost item-total F1 0.958 -> 0.935 (long
    # prompt) / 0.942 (one-line prompt), with item rows inflating 8.2 -> 8.9 per
    # call both times. Classifying the charge pulled attention off transcribing
    # the items. `ledger.adjustment_type` derives the type from provenance
    # instead, for free.
    # ponytail: retry only if the item-extraction cost can be shown to be gone.


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
