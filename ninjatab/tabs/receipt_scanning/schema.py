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
    # model has already transcribed. Item-level savings are NOT recorded here -
    # every discount/saving row goes into the receipt-level `adjustments` list
    # (with relates_to_item / related_item_index pointing back at the item it
    # belongs to); the server attributes and folds them in during post-processing.
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
    post_discount_line_total: Optional[str] = Field(
        default=None,
        description=(
            "The final per-line price actually charged when the receipt prints a discounted/"
            "charged price directly on the item row (e.g. a loyalty/Clubcard/CC price like "
            "'Cc £5.25'). Transcribe that printed charged amount here. For an item with no "
            "printed discounted price, set it equal to pre_discount_line_total. Leave null only "
            "when neither a regular nor a charged price is printed. Do NOT subtract savings "
            "yourself - separate saving rows go in `adjustments` and the server folds them in. "
            "It includes sales tax only when tax is baked into the line; tax added separately is "
            "a receipt-level adjustment."
        ),
    )


class _AdjustmentKind(str, Enum):
    TAX = "tax"
    TIP = "tip"  # tip / gratuity / service charge are one concept
    DISCOUNT = "discount"
    FEE = "fee"
    OTHER = "other"


class _Adjustment(BaseModel):
    """A charge or discount printed on the receipt that is not a purchased good.

    This covers BOTH receipt-level (basket-level) entries - tax, tip, fees, whole-
    order discounts - AND item-level savings (Special Offer, multi-buy, Clubcard/CC).
    Item-level savings are flagged with relates_to_item=true and point back at the
    item they belong to via related_item_index; the server folds them into that
    item's price during post-processing. This keeps attribution out of generation."""
    name: str = Field(description="The adjustment / saving label exactly as printed on the receipt.")
    translated_name: str = Field(description="English translation of the label (equal to name if already English).")
    kind: _AdjustmentKind = Field(
        description=(
            "The kind of adjustment: 'tax' (sales tax/VAT added on top of the items), 'tip' (a "
            "tip, gratuity, or service charge - all the same concept), 'discount' (ANY saving/"
            "reduction, whether a whole-order loyalty/membership/staff/military discount or "
            "coupon/voucher, OR an item-specific 'Special Offer', promotion, multi-buy, or "
            "Clubcard/CC saving), 'fee' (delivery/booking/cover charge), or 'other'."
        ),
    )
    amount: str = Field(
        description=(
            "The signed amount as a decimal string: NEGATIVE for subtractive adjustments "
            "(discounts, coupons, vouchers, savings) and POSITIVE for additive adjustments "
            "(tax, tip, service charge, fees). A saving is always negative even when the printed "
            "figure has no minus sign (the receipt's minus is often lost)."
        ),
    )
    relates_to_item: bool = Field(
        default=False,
        description=(
            "True when this adjustment is a saving/charge tied to ONE specific purchased item "
            "(e.g. a 'Special Offer', multi-buy/'Multi-save', or Clubcard/CC saving printed "
            "directly under an item). False for genuinely receipt-level entries (tax, tip, "
            "fees, whole-order discounts) that are not attributable to a single item. When "
            "true, set related_item_index to that item's position."
        ),
    )
    related_item_index: Optional[int] = Field(
        default=None,
        description=(
            "When relates_to_item is true, the 0-based index of the item in the `items` list "
            "that this saving/charge belongs to (the item printed directly above a saving row "
            "is the one it applies to; for a multi-buy spanning several rows, the last "
            "qualifying row). Leave null when relates_to_item is false, or when the saving "
            "cannot be confidently attributed to a specific item."
        ),
    )


class _Document(BaseModel):
    receipt_language: str = Field(description="The detected receipt language name ('English' if English, otherwise the language name).")
    receipt_language_code: Optional[str] = Field(default=None, description="ISO-style language code (e.g. 'en', 'fr', 'de'); null if not confidently known.")
    items: list[_Item]
    adjustments: Optional[list[_Adjustment]] = Field(
        default=None,
        description=(
            "Every printed charge or saving that is not itself a purchased good. This includes "
            "BOTH receipt-level entries - sales tax added on top of the items, a tip/gratuity/"
            "service charge, whole-order discounts/coupons/vouchers (loyalty/membership/staff/"
            "military), and fees (delivery, booking, cover) - AND item-level savings such as a "
            "'Special Offer'/promotion/multi-buy/Clubcard saving printed against a specific "
            "item. Flag each item-level saving with relates_to_item=true and point it at its "
            "item via related_item_index; leave receipt-level entries relates_to_item=false. The "
            "server attributes the item-level ones and folds them into the item's price. Each "
            "amount is additive (positive) or subtractive (negative). Do NOT include: VAT/tax "
            "already baked into item line totals, or cash-rounding lines. Leave null if the "
            "receipt has neither charges nor savings."
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
