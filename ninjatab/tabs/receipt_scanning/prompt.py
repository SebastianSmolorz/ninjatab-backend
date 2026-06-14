DOCUMENT_ANNOTATION_PROMPT = """
Extract structured data from this receipt.

Detect and extract the receipt language into receipt_language.
- If the receipt is in English, set receipt_language to "English".
- Otherwise set it to the detected language name.

Detect and extract the receipt language code into receipt_language_code when possible.
- Use an ISO-style language code such as "en", "fr", "es", "de", "it", etc.
- If the language code cannot be confidently determined, return null.

Before extracting items, first identify purchased item blocks.

A purchased item block is one purchased good or service plus any immediately associated physical receipt rows printed next to it or underneath it. These associated rows may include modifiers, descriptions, item codes, quantity details, item-specific discounts/savings, or loyalty/Clubcard/CC prices.

Therefore, one output item may correspond to multiple physical printed rows on the receipt. The main purchased item row and its directly attached discount/saving/loyalty-price row(s) should be treated as one item block, not as separate items.

Extract all purchased goods or services that contribute to the receipt total into items.
Do not include receipt-level charges such as tax, tip, service charge, or other fees/discounts in items - those are captured separately as receipt-level adjustments below. Item-specific discounts/savings that clearly belong to a purchased item are captured inside that item's `discount` field.

CRITICAL - one output item per primary purchased item row / purchased item block. Do not create one output item for every physical printed line.

Never merge, combine, deduplicate, or consolidate multiple primary purchased item rows into a single output item, even if those rows have the identical name and identical price. If the receipt prints "Coke 3.00" on three separate primary purchased item rows, return three separate items each with quantity 1 and total 3.00 - do NOT return a single item with quantity 3 and total 9.00, and do NOT return a single item with quantity 1 and total 9.00.

However, physical rows printed directly next to or underneath a primary purchased item row may belong to that same item block. This is especially common for item-level discounts, loyalty savings, Clubcard/CC savings, promotions, or special offers. These associated rows should be included in the relevant item's `receipt_line_text` and, where applicable, in its `discount` field. Do NOT count these associated discount/saving/loyalty-price rows as separate purchased items.

The output should have the same number of items as the receipt has primary purchased item rows / purchased item blocks, not necessarily the same number of physical printed text rows.

Only use quantity > 1, and a correspondingly larger total, when a SINGLE primary purchased item row on the receipt itself shows an explicit quantity multiplier, for example "3 x Coke   9.00".

For each item:
- name: the item name exactly as it appears on the receipt, in its original language, but excluding pricing markers such as "Cc £5.25", "Clubcard Price £5.25", "Special Offer", "Promotion", "Saving", or similar discount/loyalty text
- translated_name: the English translation of the item name
  - Always attempt a translation when the item is not already in English, even if the original text is abbreviated, partially illegible, or you have to make your best guess from context (cuisine type, common menu items, surrounding items, the establishment name)
  - For abbreviated item names (e.g. "BIRRA DIAMOND GRAN", "ANT. PIEVE VECCHIA"), expand and translate the likely full meaning ("Diamond beer (large)", "Antipasto Pieve Vecchia")
  - Only fall back to copying the original name verbatim if you genuinely cannot make any reasonable guess at the English meaning
  - If the item is already in English, set translated_name equal to name
  - Be aggressive here: the precision/conservatism rules that apply to amounts, items, and dates do NOT apply to translated_name - always produce a best-guess English translation rather than leaving it untranslated
- quantity, price_per_quantity, pre_discount_line_total, post_discount_line_total: see below
- discount: see the item-level discount guidance below

Only include price_per_quantity and quantity if clearly on the receipt.
quantity: number of instances of this item purchased. Set to 1 if it is not clear.
price_per_quantity: the price of this item per quantity.
pre_discount_line_total: the printed main regular/pre-discount price of that purchased item block BEFORE any item-level discount. This is usually the higher positive amount printed on the primary item row. Do not subtract any associated discount/saving/loyalty-price row from this value yourself - record the saving separately in `discount`. Leave pre_discount_line_total as the printed pre-discount price.
post_discount_line_total: the line total actually charged for that purchased item block AFTER its item-level discount(s). When the receipt prints the discounted/charged price directly (for example a loyalty/Clubcard/CC price such as "Cc £5.25" or "Cc 55p"), transcribe that printed charged amount here. For an item with NO item-level discount, set post_discount_line_total equal to pre_discount_line_total. If the receipt prints only a saving line (e.g. "-1.00") and no separate charged price, leave post_discount_line_total null - do NOT copy pre_discount_line_total into it - and just record the printed saving in `discount`. The server will derive the charged amount by subtracting the saving. This is the value that contributes to items_total.
receipt_line_text: the raw, verbatim text of the full purchased item block this item was extracted from, exactly as it appears. Include the primary item row and any immediately associated physical rows next to it or underneath it, including item-specific discount/saving/loyalty-price rows. If the item block spans multiple printed rows, join them with " / ". This is used to verify the extraction against the receipt - copy the source text faithfully, do not clean it up.

Item-level discounts (discount):
Many receipts apply a saving to one specific item - for example a supermarket loyalty/Clubcard saving, a "Special Offer", or a promotion. It is common for SEVERAL items on the same receipt to each carry their own discount - do NOT assume discounts are rare or that only one item is discounted. Capture every item-level saving you can confidently attribute.

When reading the receipt, scan locally around each primary purchased item row. If one or more saving rows or loyalty-price markers appear directly next to it or immediately underneath it, and those rows are labelled like a loyalty saving, Clubcard/CC price, promotion, special offer, voucher, discount, or saving, treat those rows as part of the item block unless there is clear evidence they are basket-level discounts.

For this purpose, the phrase "line item" means the whole purchased item block, not just one physical printed row.

- An item-level discount is often a NEGATIVE amount printed on the row directly UNDERNEATH that item's price, in the same price column as the item totals (e.g. an item "Bananas 10.00" with "CCC -1.00" on the next row, meaning Bananas was reduced by 1.00). The saving row is usually labelled with a loyalty/Clubcard marker (e.g. "Cc", "CCC", "Clubcard Price"), "Special Offer", "Promotion", "Saving", "Voucher", or a multi-buy promotion label such as "Multibuy", "Multi-save", "Multi-buy Saving", "Mix & Match", "Meal Deal", or "2 for £3" / "3 for 2".
- An item-level discount may also be implied by a printed loyalty/Clubcard/CC discounted price next to the item name. For example, Tesco-style text such as "Cc £5.25", "Clubcard £5.25", "Clubcard Price £5.25", or "CC 55p" usually means the discounted price actually charged for that item.
- Only populate `discount` with a saving amount that is LITERALLY PRINTED on the receipt as its own figure (e.g. a "-1.00", "Saving 1.00", or "Cc -1.15" row). A saving is always a REDUCTION, so always record it as a NEGATIVE decimal string - even when the printed figure has no minus sign in front of it. The receipt's leading minus is frequently lost in transcription, so a saving / Special Offer / Clubcard figure attached to an item is negative regardless of how its sign came through (e.g. a "Special Offer 2.40" row under an item becomes "-2.40"). NEVER calculate a discount by subtracting one printed price from another; only transcribe the printed figure - but always give it a negative sign.
- If an item block shows a regular/pre-discount price AND a separate printed loyalty/Clubcard/CC charged price, but NO explicitly printed saving figure, transcribe both prices only: pre_discount_line_total = the regular price, post_discount_line_total = the printed charged price, and leave `discount` null. Do not compute the saving yourself - the server derives it from the two printed prices.
- If only a regular price AND a printed saving figure are shown (no separate charged price), set pre_discount_line_total = the regular price, put the printed saving in `discount`, and leave post_discount_line_total NULL. Do not subtract - the server computes the charged amount.
- Convert pence values such as "55p" to decimal strings such as "0.55" when transcribing, and apply the negative sign to savings as described above, but do not otherwise compute or change the magnitude of the printed figures.
- Do NOT include "Cc £5.25", "Clubcard £5.25", "Clubcard Price £5.25", "Cc 55p", or similar loyalty price markers in the item name. They are pricing/discount information, not part of the product name.
- When a saving line clearly belongs to the item directly above it, record the printed or derived saving in THAT item's `discount` list as a NEGATIVE decimal string (e.g. ["-1.00"]). Do NOT create a separate item for the saving line, do NOT add it to adjustments, do NOT merge the saving's label or amount into the item's name, and do NOT subtract it from pre_discount_line_total yourself - leave pre_discount_line_total as the printed pre-discount price and put the charged amount in post_discount_line_total.
- `discount` is a LIST. If a single item shows more than one separate saving line that each clearly apply to it, record EACH printed saving as its own negative string entry in that item's `discount` list (e.g. ["-1.00", "-0.50"]). Do not add them together yourself - record each printed amount separately. One saving per item is the most common case, in which the list has a single entry. An item carrying TWO discount rows (for example a Clubcard/CC loyalty price AND a separate "Special Offer" saving row, or two "Special Offer" rows) is normal - roll EVERY such row into that one item's `discount` list. A second discount row on an item is NOT a reason to give up on attribution or to push any of those savings into adjustments.
- Multi-buy / multi-save promotions (e.g. "Multi-save", "Multibuy", "Mix & Match", "Meal Deal", "2 for £3", "3 for 2") are ITEM-LEVEL discounts, NOT basket/receipt-level discounts, even though they reference more than one unit. The single printed saving row usually appears immediately UNDERNEATH the group of qualifying item rows (often two or more rows of the same product). Attach the WHOLE printed saving amount to the `discount` list of the item block printed directly above the saving line (the last qualifying row). Do NOT split the saving across the qualifying rows (that would be arithmetic), do NOT drop it, and do NOT move it to adjustments just because it spans several units.
- A discount may be printed as a STANDALONE row of its own, where one line shows only a promotion label such as "Special Offer", "Saving", or "Promotion" with its amount sitting in the price column, printed directly UNDERNEATH the item it reduces (e.g. an item "Steaks 8.00" followed by a "Special Offer 2.40" row means the steaks were reduced by 2.40). Roll that row up into the item directly above it: add its amount to that item's `discount` list as a negative, and include its text in that item's `receipt_line_text`. A "Special Offer" / promotion printed against a specific item is ALWAYS an item-level discount - never emit it as its own item and never move it to adjustments.
- Prefer the explicit negative saving amount printed on the receipt when it is clearly visible and clearly attached to the item.
- If no explicit negative saving is visible, but a regular price and a loyalty/Clubcard/CC discounted price are both printed for the same item block, derive the discount from those two printed prices.
- If a saving line or loyalty price cannot be confidently attributed to a specific item OR to a specific adjacent group of qualifying items (as with a multi-buy), and is not a whole-basket discount as described below, do NOT extract it at all - leave it out entirely rather than guessing which item it belongs to. A multi-buy saving printed directly under its qualifying item rows IS confidently attributable - attach it to the item directly above per the multi-buy rule.
- Leave `discount` null for items with no item-specific saving.

Tesco / Clubcard examples:

Printed/OCR text:
1 | Monster Energy Drink Ultra 4x500ml Cc £5.25 | £6.40

Output:
{
  "name": "Monster Energy Drink Ultra 4x500ml",
  "translated_name": "Monster Energy Drink Ultra 4x500ml",
  "quantity": 1,
  "price_per_quantity": null,
  "pre_discount_line_total": "6.40",
  "post_discount_line_total": "5.25",
  "discount": ["-1.15"],
  "receipt_line_text": "1 | Monster Energy Drink Ultra 4x500ml Cc £5.25 | £6.40"
}

Printed/OCR text:
1 | 7 Days Double Croissant - Cocoa & Vanilla Flavour Fillings 60g Cc 55p | £0.70

Output:
{
  "name": "7 Days Double Croissant - Cocoa & Vanilla Flavour Fillings 60g",
  "translated_name": "7 Days Double Croissant - Cocoa & Vanilla Flavour Fillings 60g",
  "quantity": 1,
  "price_per_quantity": null,
  "pre_discount_line_total": "0.70",
  "post_discount_line_total": "0.55",
  "discount": ["-0.15"],
  "receipt_line_text": "1 | 7 Days Double Croissant - Cocoa & Vanilla Flavour Fillings 60g Cc 55p | £0.70"
}

Multi-buy example (saving spans two qualifying rows):

Printed/OCR text:
1 | AMB RICE POT | £1.10
1 | AMB RICE POT | £1.10
  | Multi-save | £-0.45

Here the "Multi-save £-0.45" is item-level, not basket-level. Extract two AMB RICE POT items, and apply the whole "-0.45" saving as a `discount` on the single row directly above the saving line (the second AMB RICE POT). Do not split it across both rows and do not put it in adjustments.

Standalone "Special Offer" row example (saving printed under its item, minus sign often lost):

Printed/OCR text:
1 | Tesco Finest Ranch Steaks 303g | £8.00
  | Special Offer | £2.40

Here the "Special Offer 2.40" row is the saving for the steaks directly above it. Even though it is printed without a minus, it is a reduction. Output one steaks item with the saving rolled up:
{
  "name": "Tesco Finest Ranch Steaks 303g",
  "translated_name": "Tesco Finest Ranch Steaks 303g",
  "quantity": 1,
  "price_per_quantity": null,
  "pre_discount_line_total": "8.00",
  "post_discount_line_total": null,
  "discount": ["-2.40"],
  "receipt_line_text": "1 | Tesco Finest Ranch Steaks 303g | £8.00 / Special Offer | £2.40"
}
Do NOT emit "Special Offer" as its own item and do NOT put the 2.40 in adjustments - it belongs on the steaks. The same holds when an item has two such rows (e.g. a "Cc" loyalty price AND a "Special Offer"): roll both into that item's `discount` list.

Do not output "Cc £5.25", "Cc 55p", "Clubcard Price", "Special Offer", "Multi-save", or similar pricing/discount text as separate items. Do not put item-specific Clubcard/CC or multi-buy savings in adjustments when they clearly belong to a purchased item or its qualifying group. Keep pre_discount_line_total as the regular/pre-discount price, put the charged amount in post_discount_line_total, and record the saving in `discount`.

Rows whose name is only a promotion/discount label, such as "Special Offer", "Promotion", "Savings", "Clubcard Price", "Cc", "Voucher", "Multi-save", "Multibuy", "Mix & Match", "Meal Deal", or similar, are not purchased goods or services.

Do not output these rows as items.

If such a row clearly belongs to the purchased item directly above it, attach it to that item's `discount` list.
If instead it is a globally-applied discount affecting the whole order (a loyalty/membership/staff/military discount, or a whole-basket coupon/voucher) that is not attributable to any single item, put it in `adjustments`.
If it is only an informational summary of savings already captured at item level, do not extract it again.

Do not include subtotal, tax, VAT, tip, gratuity, service charge, payment method, change, balance, loyalty adjustments, discounts, or any other fees as items - even if they affect the grand total. These are captured separately: item-level savings in `discount` above; receipt-level charges in `adjustments` below.

Extract receipt-level charges and discounts that affect the grand total into the `adjustments` list. An adjustment is a receipt-level (basket-level) charge or discount that is NOT tied to any specific item or group of items - for example sales tax/VAT added on top of the items, a tip/gratuity/service charge, a delivery/booking/cover fee, or a basket-level discount/voucher applied to the whole order (such as "£5 off £40 spend", a percentage off the whole bill, or a staff/loyalty discount taken off the order total).

A discount belongs in `adjustments` ONLY when it is applied GLOBALLY to the whole order (a loyalty/membership/staff/military discount, or a whole-basket coupon/voucher) and cannot be attributed to any single item. An item-specific "Special Offer", promotion, multi-buy, or Clubcard/CC saving is NOT an adjustment - roll it into the relevant item's `discount`, even when several items each carry one. If you find yourself putting a "Special Offer" or promotion in `adjustments`, that is a misclassification: it belongs on the item printed directly above it.

Each adjustment entry has:
- name: the label exactly as printed on the receipt
- translated_name: the English translation of the label (or the same value if already English)
- kind: one of "tax" (sales tax/VAT added on top of the items), "tip" (a tip, gratuity, or service charge - treat all three as the same kind), "discount" (ONLY a globally-applied discount off the whole order - a loyalty/membership/staff/military discount, or a whole-basket coupon/voucher such as "£5 off £40 spend" or a percentage off the entire bill; NEVER an item-specific "Special Offer", promotion, multi-buy, or Clubcard/CC saving attached to one product), "fee" (delivery/booking/cover charge), or "other"
- amount: a decimal string, NEGATIVE for subtractive adjustments (discounts, coupons, vouchers) and POSITIVE for additive adjustments (tax, tip, service charge, fees)

Rules for adjustments:
- Only include an adjustment when it clearly affects the grand total. Leave `adjustments` null if the receipt has none.
- Do NOT include VAT/sales tax that is already baked into the item line totals. Record tax as an adjustment only when it is added on top of the items to reach the grand total.
- Do NOT include item-specific savings here, and do NOT include "Special Offer" / promotion savings or multi-buy / multi-save / "X for Y" / mix & match / meal-deal savings here - those are item-level and belong in the relevant item's `discount` field, even when several items each carry one and even though some span more than one unit. A discount qualifies as an adjustment ONLY when it is applied globally to the whole order and cannot be attributed to a specific item.
- Do NOT include cash-rounding lines (e.g. "Rounding -0.02") as adjustments.
- Do NOT include purchased goods or services here, and do NOT include these adjustments in items.

Do not duplicate savings summaries.

If the receipt shows both "Savings" and "Promotions" with the same amount, and they appear to refer to the same overall promotional saving total, do not output both.

If item-level discounts have already been captured in item `discount` fields, do not also output the receipt's total savings/promotions summary in `adjustments`. That would double-count the same discount.

Only use `adjustments` for receipt-level fees or discounts that are separate from item-level discounts and actually need to be applied to reconcile the grand total.

Extract receipt_total as the final total charged on the receipt. If the receipt does not explicitly display a grand total, return null - do not calculate, sum, or otherwise invent a receipt_total from the items or charges.

Set receipt_total_visible to true when a grand total / amount due / total payable is clearly printed on the receipt, and false when no grand total is shown (or it is illegible or too ambiguous to read confidently). This reports only whether the printed total is legibly present - it is independent of whether the items add up to it. When receipt_total_visible is false, receipt_total must be null.

Extract receipt_establishment_name as the merchant or establishment name shown on the receipt if available.

Extract currency_code in ISO 4217 format, for example GBP, EUR, USD.
- Prefer an explicit currency symbol, code, or label printed on the receipt
- If no explicit currency is shown but the receipt's address (country/city), language, tax label (e.g. "VAT", "IVA", "MwSt", "GST"), or merchant clearly indicates a single dominant currency for that locale, set currency_code to that currency
- If the currency cannot be confidently determined from explicit markings or strong contextual evidence, return null - do not guess or invent a currency code from a weak signal

Calculate items_total as the sum of all item post_discount_line_total values.

For each item:
- If post_discount_line_total is present, use that value.
- If post_discount_line_total is missing but pre_discount_line_total is present and there are no item-level discounts, use pre_discount_line_total.
- Do not use pre_discount_line_total for items_total when an item-level discount is present.

Report the honest charged sum even if it does not match receipt_total - do not adjust, add, or drop items to force the totals to agree.

Extract datetime_of_receipt from the receipt date/time.
- Return it as an ISO 8601 string when possible
- If the receipt provides only a partial date or ambiguous date/time that cannot be confidently converted to ISO 8601, return null
- If no receipt date/time is present, return null

All monetary amounts (pre_discount_line_total, post_discount_line_total, price_per_quantity, each entry in discount, receipt_total, items_total, adjustments.amount) must be returned as decimal strings normalized to US locale formatting:
- Use a dot (".") as the decimal separator
- Do not include any thousands separators (no commas, no spaces, no dots between groups of digits)
- Use the number of decimal places appropriate for the receipt's currency: 0 for currencies with no minor unit (e.g. JPY), 2 for most currencies (e.g. USD, EUR, GBP), 3 for currencies that use three decimals (e.g. JOD, KWD, BHD, OMR, TND). Match the precision shown on the receipt itself - never truncate "1.234" (a JOD amount) to "1.23"
- Use a leading minus sign for negative amounts (discounts)
- ALWAYS preserve a minus symbol printed next to a monetary value. If the receipt shows a "-" before, after, or attached to an amount (e.g. "-£0.84", "£0.84-", "-0.84"), that value is negative - carry the sign through as a leading minus ("-0.84") and never drop it. A minus next to a price is meaningful (it marks a saving/refund/credit), not noise.

Examples: "3.50" (USD), "1234.56" (EUR), "-1.20" (discount), "0.99" (GBP), "1500" (JPY), "12.345" (JOD).
Do not return values like "1,234.56", "1.234,56", "1 234,56", "20,00", or numbers with spurious extra decimal digits beyond the currency's precision, even if the receipt itself uses those formats. Convert from the receipt's local format to US format before returning.

Be precise and conservative about monetary amounts, quantities, dates, and which items contribute to the total. Do not invent prices or items that are not on the receipt. Reminder: this conservatism does not apply to translated_name - see the translation guidance above."""


# A distilled, deliberately literal alternative to DOCUMENT_ANNOTATION_PROMPT.
# It transcribes the receipt faithfully and pushes interpretation (stripping
# promo/loyalty markers from names, classifying discounts as item- vs
# basket-level, deriving charged amounts) onto the post-processing layer. Use
# it via a strategy variant, e.g. BaselineStrategy(prompt=DOCUMENT_ANNOTATION_PROMPT_LITERAL).
DOCUMENT_ANNOTATION_PROMPT_LITERAL = """
Transcribe this receipt faithfully into structured data. Be literal: copy what is printed, do not clean it up, do not interpret, and do not do any arithmetic. The server post-processes your output, so your only job is an accurate, complete transcription.

Detect the receipt language into receipt_language ("English" if English, otherwise the language name), and its ISO code into receipt_language_code (e.g. "en", "fr", "de"); null if not confidently known.

Items - one output item per printed purchased-item row:
- Output exactly one item for each primary printed row of a purchased good or service, in the order they appear.
- NEVER merge, combine, deduplicate, or consolidate rows, even if name and price are identical. Three "Coke 3.00" rows are three items, each quantity 1, total 3.00.
- quantity is 1 unless a single row shows an explicit multiplier like "3 x Coke 9.00". price_per_quantity only if a per-unit price is printed; otherwise null.

For each item:
- name: the row's text EXACTLY as printed, in its original language, VERBATIM. Do NOT trim, clean, normalize, abbreviate, or remove anything. Keep all printed markers - loyalty/Clubcard/CC prices ("Cc £5.25"), "Special Offer", "Multi-save", item codes, weights, modifiers - as part of the name. Faithful transcription is the priority; the server strips markers later.
- translated_name: a best-guess English translation of the item (or equal to name if already English). Be aggressive about translating even abbreviated names; this is the one field where guessing is encouraged.
- pre_discount_line_total: the line total printed for that row, transcribed verbatim. This is the only price field you must fill for a normal item.
- post_discount_line_total: leave null. The server derives the charged amount.
- discount: leave null. Do not attach or derive item-level discounts - the server does that from the discount rows you transcribe below.
- receipt_line_text: the raw text of the row(s) this item came from, verbatim; join multiple physical rows with " / ".

Discount / saving rows (e.g. "Multi-save -0.45", "Cc -1.15", "Special Offer -1.00", "Voucher -2.00"):
- These are NOT purchased goods, so do not output them as items.
- Transcribe every such row into adjustments as a separate entry: name = the label printed verbatim, translated_name = its English translation (or same), kind = "discount", amount = the printed amount as a negative decimal string.
- Do NOT decide whether a saving is item-level or basket-level, and do NOT attach it to an item - the server attributes discounts during post-processing using the order and text you provide.

Receipt-level fields (only when printed; otherwise null):
- adjustments: in addition to the discount rows above, every printed receipt-level charge - tax/VAT, a tip/gratuity/service charge, and any other fee (delivery, booking, cover, etc.) - as {name, translated_name, kind, amount}. kind is one of "tax", "tip" (covers tip/gratuity/service charge), "discount", "fee", "other". amount is positive for charges/fees and negative for discounts. Just transcribe each printed row; do not compute or reconcile.
- receipt_total: the grand total printed on the receipt, verbatim. If no grand total is printed, null - never sum or invent it.
- receipt_establishment_name: the merchant/establishment name if shown.
- currency_code: ISO 4217 (e.g. GBP, EUR, USD), from an explicit symbol/code/label, or from a strong locale signal (address, language, tax label); null if not confident.
- datetime_of_receipt: ISO 8601 when the printed date/time converts cleanly; null for partial/ambiguous/absent.

Amount formatting - all monetary values as decimal strings in US format:
- Dot as decimal separator, no thousands separators.
- Decimal places matching the currency: 0 for JPY, 2 for most (USD/EUR/GBP), 3 for JOD/KWD/BHD/OMR/TND. Match the precision printed; do not add or drop digits.
- Leading minus for negative amounts (discounts). Convert "55p" to "0.55".
- Do not return "1,234.56", "1.234,56", or "1 234,56"; convert to US format.

Transcribe only what is printed. Do not invent, sum, subtract, or reconcile anything - all calculation and discount attribution happens in post-processing."""
