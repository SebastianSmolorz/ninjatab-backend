from typing import Optional

from pydantic import BaseModel, Field


# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server.

class _Item(BaseModel):
    name: str = Field(description="The item name exactly as printed on the receipt, in the original language.")
    translated_name: str = Field(description="English translation of the item name.")
    pre_discount_line_total: str = Field(
        description=(
            "The regular / pre-discount line total for this item block (usually "
            "quantity * price_per_quantity), before any item-level discount. Always "
            "provide this - for an item with no discount it is simply the item's price."
        ),
    )
    post_discount_line_total: Optional[str] = Field(
        default=None,
        description=(
            "The line total actually charged for this item block after its item-level "
            "discount(s) - e.g. a printed Clubcard/CC price such as 'Cc 5.25'. For an item "
            "with no discount, equal to pre_discount_line_total. If only a saving line is "
            "printed (no separate charged price), leave null and the server derives it. "
            "This is the value summed into items_total."
        ),
    )
    quantity: Optional[int] = Field(default=None, description="Quantity if clearly shown; otherwise null.")
    price_per_quantity: Optional[str] = Field(default=None, description="Unit price if clearly shown; otherwise null.")
    discount: Optional[list[str]] = Field(
        default=None,
        description=(
            "Item-specific discount/saving amounts printed directly under this item, "
            "each as a negative decimal string, e.g. ['-1.00']. "
            "Leave null if this item has no item-specific saving."
        ),
    )
    receipt_line_text: Optional[str] = Field(
        default=None,
        description=(
            "Raw OCR text of the full purchased item block, including the primary item row "
            "and any directly attached discount/saving rows underneath it. Join multiple "
            "physical receipt rows with ' / '."
        ),
    )


class _OtherCharge(BaseModel):
    name: str
    translated_name: str
    amount: str


class _Document(BaseModel):
    receipt_language: str
    receipt_language_code: Optional[str] = None
    items: list[_Item]
    receipt_total: Optional[str] = None
    receipt_total_visible: Optional[bool] = Field(
        default=None,
        description=(
            "True if a grand total / amount due is clearly printed on the receipt; "
            "False if no grand total is shown or it is illegible/ambiguous. Reports "
            "only whether the printed total is legibly present, independent of whether "
            "the items reconcile to it. When False, receipt_total is null."
        ),
    )
    items_total: Optional[str] = None
    receipt_establishment_name: Optional[str] = None
    currency_code: Optional[str] = None
    datetime_of_receipt: Optional[str] = None
    tax: Optional[str] = None
    tip: Optional[str] = None
    service_charge: Optional[str] = None
    other_charges: Optional[list[_OtherCharge]] = None
