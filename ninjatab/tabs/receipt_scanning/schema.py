from typing import Optional

from pydantic import BaseModel, Field

# Thoughts for input tags when creating training/validation set. Some of these may be valuable as output in schema too?
# - items_include_tax
# - displays_receipt_level_tip
# - displays_receipt_level_tax_which_changes_grand_total (USA)
# - displays_receipt_level_discount
# - has_item_level_discounts (item, -discount $)
# - has_item_with_discount_price_visible (normally $x, multibuy $y)
# - total not visible
# - blurry

# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server.

class _Item(BaseModel):
    name: str = Field(description="The item name exactly as printed on the receipt, in the original language.")
    translated_name: str = Field(description="English translation of the item name.")
    pre_discount_line_total: Optional[str] = Field(
        default=None,
        description=(
            "The total amount of the pre discount line total, quantity * price per quantity in the original language."
            "Do not invent this. It may not be visible. "
        ),

    )

    line_paid_total: str = Field(
        description=(
            "The final total actually paid for this line. It typically represents: ((quantity * price per quantity) - line level discounts)."
            "In some cases this will include any sales tax, in other cases tax is added on separately to the receipt grand total."
        ),
    )
    quantity: Optional[int] = Field(default=None, description="Quantity if clearly shown; otherwise null.")
    price_per_quantity: Optional[str] = Field(default=None, description="Unit price if clearly shown; otherwise null.")
    discount: Optional[list[str]] = Field(
        default=None,
        description=(
            "Item-specific discount/saving amounts. Typically printed directly under this item, "
            "as a negative decimal e.g. -1.00. "
            "Leave null if this item has no visible and obvious item-specific saving."
        ),
    )
    receipt_line_text: Optional[str] = Field(
        default=None,
        description=(
            "Raw OCR text of the full purchased item block, including the primary item row "
            "and any directly attached discount/saving rows or other descriptive data. Join multiple "
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
    # This is the amount actually required to pay on the receipt
    receipt_grand_total: Optional[str] = None
    receipt_grand_total_visible: Optional[bool] = Field(
        default=False,
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
    # deprecated - to be included as adjustments. But need to think about how to populate existing tip UI or replace it.
    # tax: Optional[str] = None
    # tip: Optional[str] = None
    service_charge: Optional[str] = None
    other_charges: Optional[list[_OtherCharge]] = None

    # This needs to become a new model I think to hold info on discount/addition etc.
    # Suggested description:
    # An adjustment may be either additive or subtractive.
    # It exists at the receipt level only. Not tied to any specific line item
    # thus must necessarily affect the grand total paid.
    # Do not include VAT if it is part of the line item total already.
    # Subtractive adjustments such as coupons, member discounts etc. are often
    # represented as negative values but not always.
    # Additive adjustments can represent sales tax or tips/gratuity/service charges.
    adjustments: Optional[list] = None
# todo tighten up the terminology between items and lines/line items.

#     todo (claude don't implement this) when a discount comes in, in post processing run an abs() through it and return to app normalised