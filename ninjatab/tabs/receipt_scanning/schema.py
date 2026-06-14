from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# This schema is the constrained-decoding contract handed to Mistral via
# response_format_from_pydantic_model() (see base.py). Field NAMES here are the
# exact keys the model emits and that postprocess.py reads, so they must stay in
# lockstep with the post-processor's vocabulary. Field DESCRIPTIONS are seen by
# the model at generation time - keep them explicit and self-contained.
#
# Monetary amounts are typed as `str` rather than `float` to prevent the
# constrained JSON decoder from runaway-sampling decimal digits and truncating
# the response. We coerce to float on the server.

# Thoughts for input tags when creating training/validation set. Some of these may be valuable as output in schema too?
# - items_include_tax
# - displays_receipt_level_tip
# - displays_receipt_level_tax_which_changes_grand_total (USA)
# - displays_receipt_level_discount
# - has_item_level_discounts (item, -discount $)
# - has_item_with_discount_price_visible (normally $x, multibuy $y)
# - total not visible
# - blurry


class _Item(BaseModel):
    # Field order is deliberately transcribe-then-parse: the raw line text is
    # committed first so the parsed numeric fields below are read off text the
    # model has already transcribed, and pre -> discount -> post mirrors the
    # arithmetic dependency (paid = pre - discount).
    receipt_line_text: Optional[str] = Field(
        default=None,
        description=(
            "Raw OCR text of the full purchased item block, transcribed verbatim: the primary "
            "item row plus any directly attached discount/saving/loyalty-price rows. Join "
            "multiple physical receipt rows with ' / '. Transcribe this first, then read the "
            "parsed fields below off it."
        ),
    )
    name: str = Field(
        description=(
            "The item name exactly as printed on the receipt, in the original language. "
            "Exclude pricing/discount markers such as 'Cc £5.25', 'Clubcard Price', "
            "'Special Offer', 'Multi-save'."
        ),
    )
    translated_name: str = Field(description="English translation of the item name (equal to name if already English).")
    quantity: Optional[int] = Field(
        default=None,
        description="Quantity only if a single row shows an explicit multiplier (e.g. '3 x Coke 9.00'); otherwise null (treated as 1).",
    )
    price_per_quantity: Optional[str] = Field(default=None, description="Unit price if clearly printed; otherwise null.")
    pre_discount_line_total: Optional[str] = Field(
        default=None,
        description=(
            "The printed regular / pre-discount line total for this item block (typically "
            "quantity * price_per_quantity), BEFORE any item-level discount - the higher "
            "printed amount when a discounted price is also shown. Transcribe what is printed; "
            "do not subtract discounts yourself. May not be visible - leave null if so, do not "
            "invent it."
        ),
    )
    discount: Optional[list[str]] = Field(
        default=None,
        description=(
            "Item-level savings printed on the receipt for THIS item, each as a negative "
            "decimal string (e.g. ['-1.00'], or ['-1.00', '-0.50'] when several savings apply "
            "to one item). Includes loyalty/Clubcard/CC, multi-buy, and standalone 'Special "
            "Offer'/promotion rows printed directly under this item. A saving is always a "
            "reduction, so record it negative even when the printed figure has no minus sign "
            "(the receipt's minus is often lost). Roll up every such row, including when this "
            "item carries more than one. Never compute a saving by subtracting one printed "
            "price from another. Leave null if this item has no visible item-specific saving."
        ),
    )
    post_discount_line_total: Optional[str] = Field(
        default=None,
        description=(
            "The final total actually PAID for this line (the line's paid total): "
            "((quantity * price_per_quantity) - item-level discounts). Transcribe it ONLY when "
            "a charged/discounted price is printed directly (e.g. a loyalty/Clubcard/CC price). "
            "For an item with no item-level discount, set it equal to pre_discount_line_total. "
            "If only a saving line is printed (no separate charged price), leave this null - the "
            "server derives the paid amount. This is the value that contributes to items_total. "
            "It includes sales tax only when tax is baked into the line; leave tax to receipt-"
            "level adjustments when it is added separately."
        ),
    )


class _AdjustmentKind(str, Enum):
    TAX = "tax"
    TIP = "tip"  # tip / gratuity / service charge are one concept
    DISCOUNT = "discount"
    FEE = "fee"
    OTHER = "other"


class _Adjustment(BaseModel):
    """A receipt-level (basket-level) charge or discount. By definition it is not
    tied to any specific item, so it necessarily affects the grand total paid."""
    name: str = Field(description="The adjustment label exactly as printed on the receipt.")
    translated_name: str = Field(description="English translation of the label (equal to name if already English).")
    kind: _AdjustmentKind = Field(
        description=(
            "The kind of receipt-level adjustment: 'tax' (sales tax/VAT added on top of the "
            "items), 'tip' (a tip, gratuity, or service charge - all the same concept), "
            "'discount' (ONLY a globally-applied discount off the whole order - a loyalty/"
            "membership/staff/military discount or a whole-basket coupon/voucher; NEVER an "
            "item-specific 'Special Offer', promotion, multi-buy, or Clubcard/CC saving, which "
            "belong in the relevant item's `discount`), 'fee' (delivery/booking/cover charge), "
            "or 'other'."
        ),
    )
    amount: str = Field(
        description=(
            "The signed amount as a decimal string: NEGATIVE for subtractive adjustments "
            "(discounts, coupons, vouchers) and POSITIVE for additive adjustments (tax, tip, "
            "service charge, fees)."
        ),
    )


class _Document(BaseModel):
    receipt_language: str = Field(description="The detected receipt language name ('English' if English, otherwise the language name).")
    receipt_language_code: Optional[str] = Field(default=None, description="ISO-style language code (e.g. 'en', 'fr', 'de'); null if not confidently known.")
    items: list[_Item]
    adjustments: Optional[list[_Adjustment]] = Field(
        default=None,
        description=(
            "Receipt-level (basket-level) charges and discounts that affect the grand total and "
            "are NOT tied to any specific item: sales tax added on top of the items, a tip/"
            "gratuity/service charge, globally-applied discounts/coupons/vouchers (loyalty/"
            "membership/staff/military, or a whole-order coupon), and fees (delivery, booking, "
            "cover). An adjustment is additive (positive amount) or subtractive (negative "
            "amount). Do NOT include: VAT/tax already baked into item line totals; item-level "
            "savings such as a 'Special Offer'/promotion/multi-buy/Clubcard saving attached to "
            "a specific item (those go in that item's `discount`, even when several items each "
            "have one); or cash-rounding lines. Leave null if the receipt has none."
        ),
    )
    receipt_total: Optional[str] = Field(
        default=None,
        description=(
            "The receipt grand total / amount due actually paid, exactly as printed. Do not "
            "calculate, sum, or invent it - if no grand total is printed, leave null."
        ),
    )
    receipt_total_visible: Optional[bool] = Field(
        default=False,
        description=(
            "True if a grand total / amount due / total payable is clearly printed on the "
            "receipt; False if no grand total is shown or it is illegible/ambiguous. Reports "
            "only whether the printed grand total is legibly present, independent of whether "
            "the items reconcile to it. When False, receipt_total must be null."
        ),
    )
    # items_total: Optional[str] = Field(
    #     default=None,
    #     description=(
    #         "The model's own sum of all item post_discount_line_total values, as a cross-check. "
    #         "The server recomputes this independently; report the honest charged sum even if it "
    #         "does not match receipt_total - do not adjust items to force agreement."
    #     ),
    # )
    receipt_establishment_name: Optional[str] = Field(default=None, description="The merchant / establishment name shown on the receipt, if available.")
    currency_code: Optional[str] = Field(default=None, description="ISO 4217 currency code (e.g. GBP, EUR, USD); null if it cannot be confidently determined.")
    datetime_of_receipt: Optional[str] = Field(default=None, description="The receipt date/time as an ISO 8601 string; null if partial, ambiguous, or absent.")

# todo tighten up the terminology between items and lines/line items.

# todo (claude don't implement this) when a discount comes in, in post processing run an abs() through it and return to app normalised
