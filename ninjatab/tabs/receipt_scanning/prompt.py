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
Do not include charges or savings such as tax, tip, service charge, fees, or discounts in items - every one of those is captured separately in the `adjustments` list below. This includes item-specific savings: an item never carries its own discount field. A saving that clearly belongs to one item is still recorded as an adjustment, just flagged (relates_to_item / related_item_index) so the server can fold it back into that item.

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
Items never carry their own discount field. Every saving is recorded in `adjustments`, even when it clearly belongs to one item (see "Discounts and savings" below).

Only include price_per_quantity and quantity if clearly on the receipt.
quantity: number of instances of this item purchased. Set to 1 if it is not clear.
price_per_quantity: the price of this item per quantity.
pre_discount_line_total: the printed main regular/pre-discount price of that purchased item block BEFORE any item-level discount. This is usually the higher positive amount printed on the primary item row. Do not subtract any associated discount/saving/loyalty-price row from this value yourself - record the saving separately in `adjustments`. Leave pre_discount_line_total as the printed pre-discount price.
post_discount_line_total: only set this when a discounted/charged price is printed DIRECTLY on the item row itself (for example a loyalty/Clubcard/CC price such as "Cc £5.25" or "Cc 55p" printed next to the item, alongside its higher regular price). Transcribe that printed charged amount here, and leave the saving to be derived from the pre/post gap - do NOT also add a separate adjustment for it. For an item with NO discount at all, set post_discount_line_total equal to pre_discount_line_total. When the saving is instead printed as its OWN separate row (e.g. "Special Offer 2.40", "Multi-save -0.45"), leave post_discount_line_total null and record that saving as an adjustment - the server derives the charged amount.
receipt_line_text: the raw, verbatim text of the full purchased item block this item was extracted from, exactly as it appears. Include the primary item row and any immediately associated physical rows next to it or underneath it, including item-specific discount/saving/loyalty-price rows. If the item block spans multiple printed rows, join them with " / ". This is used to verify the extraction against the receipt - copy the source text faithfully, do not clean it up.

Discounts and savings:
Every discount or saving printed on the receipt goes into the `adjustments` list - never on the item itself. This includes whole-order discounts AND item-specific savings (loyalty/Clubcard, "Special Offer", multi-buy). For an item-specific saving, set relates_to_item=true and related_item_index to the 0-based position of the item it belongs to in the `items` list; the server folds it into that item. For a whole-order discount, set relates_to_item=false. You decide which item a saving points at; you do NOT do the arithmetic.

Many receipts apply a saving to one specific item - a supermarket loyalty/Clubcard saving, a "Special Offer", or a promotion. It is common for SEVERAL items on the same receipt to each have their own saving - do NOT assume discounts are rare. Capture every saving you can see, and attribute each to its item when you confidently can.

When reading the receipt, scan locally around each primary purchased item row for saving rows or loyalty-price markers printed directly next to it or immediately underneath it (labelled like a loyalty saving, Clubcard/CC price, promotion, special offer, voucher, discount, saving, or a multi-buy label such as "Multibuy", "Multi-save", "Mix & Match", "Meal Deal", "2 for £3", "3 for 2").

- A saving is often a NEGATIVE amount printed on the row directly UNDERNEATH that item's price, in the same price column (e.g. "Bananas 10.00" with "CCC -1.00" on the next row). Record it as one adjustment with relates_to_item=true and related_item_index pointing at the Bananas item.
- A standalone promotion row, where a line shows only a label such as "Special Offer", "Saving", or "Promotion" with its amount in the price column directly UNDERNEATH the item it reduces (e.g. "Steaks 8.00" then "Special Offer 2.40"), is an item-level saving for the item above it: one adjustment, relates_to_item=true, pointing at the steaks.
- Multi-buy / multi-save promotions (e.g. "Multi-save", "Multibuy", "Mix & Match", "Meal Deal", "2 for £3", "3 for 2") are item-level even though they reference more than one unit. The single printed saving row usually appears immediately UNDERNEATH the group of qualifying item rows. Record the WHOLE printed saving as one adjustment with relates_to_item=true, related_item_index pointing at the LAST qualifying row. Do NOT split it across the rows.
- An item may carry MORE than one saving (e.g. a Clubcard price AND a "Special Offer"). Record each printed saving as its own adjustment entry, each pointing at that same item's index. Do not add them together.
- A saving is always a REDUCTION, so record its amount as a NEGATIVE decimal string - even when the printed figure has no minus sign (the receipt's leading minus is frequently lost; a "Special Offer 2.40" row becomes "-2.40"). NEVER calculate a saving by subtracting one printed price from another; only transcribe the printed figure, with a negative sign. Convert pence values such as "55p" to "0.55".
- The printed-charged-price case is the ONE exception that does not produce an adjustment: when an item row shows BOTH a regular price AND a loyalty/Clubcard/CC charged price (e.g. "... Cc £5.25 | £6.40"), set pre_discount_line_total = the regular price and post_discount_line_total = the charged price, and do NOT also add a discount adjustment - the server derives the saving from the two prices.
- If a saving cannot be confidently attributed to a specific item, set relates_to_item=false and leave related_item_index null - record it as a plain receipt-level discount rather than guessing which item it belongs to.
- Do NOT include loyalty price markers ("Cc £5.25", "Clubcard Price", "Cc 55p", "Special Offer", "Multi-save") in the item name. They are pricing/discount information, not part of the product name.
- Do NOT emit a saving row as its own item, and do NOT merge its label or amount into an item's name.

Tesco / Clubcard examples:

Printed/OCR text (charged price printed on the item row - no adjustment, use pre/post):
1 | Monster Energy Drink Ultra 4x500ml Cc £5.25 | £6.40

Output item:
{
  "name": "Monster Energy Drink Ultra 4x500ml",
  "translated_name": "Monster Energy Drink Ultra 4x500ml",
  "quantity": 1,
  "price_per_quantity": null,
  "pre_discount_line_total": "6.40",
  "post_discount_line_total": "5.25",
  "receipt_line_text": "1 | Monster Energy Drink Ultra 4x500ml Cc £5.25 | £6.40"
}
(No adjustment: the server derives the -1.15 saving from 6.40 vs 5.25.)

Multi-buy example (saving spans two qualifying rows). Say these are items at index 4 and 5:
1 | AMB RICE POT | £1.10
1 | AMB RICE POT | £1.10
  | Multi-save | £-0.45

Extract two AMB RICE POT items (pre_discount_line_total "1.10" each, no post), and ONE adjustment:
{
  "name": "Multi-save",
  "translated_name": "Multi-save",
  "kind": "discount",
  "amount": "-0.45",
  "relates_to_item": true,
  "related_item_index": 5
}
The whole -0.45 points at the last qualifying row (index 5). Do not split it across both rows.

Standalone "Special Offer" row example (saving printed under its item, minus sign often lost). Say the steaks are item index 2:
1 | Tesco Finest Ranch Steaks 303g | £8.00
  | Special Offer | £2.40

Extract one steaks item (pre_discount_line_total "8.00", no post), and ONE adjustment:
{
  "name": "Special Offer",
  "translated_name": "Special Offer",
  "kind": "discount",
  "amount": "-2.40",
  "relates_to_item": true,
  "related_item_index": 2
}
Even though printed without a minus, "Special Offer 2.40" is a reduction. Do NOT emit it as its own item.

Rows whose name is only a promotion/discount label, such as "Special Offer", "Promotion", "Savings", "Clubcard Price", "Cc", "Voucher", "Multi-save", "Multibuy", "Mix & Match", "Meal Deal", or similar, are not purchased goods or services. Do not output these rows as items - record them as adjustments.

If a savings row is only an informational summary of savings already captured as individual adjustments, do not extract it again (see "Do not duplicate savings summaries" below).

Do not include subtotal, tax, VAT, tip, gratuity, service charge, payment method, change, balance, loyalty adjustments, discounts, or any other fees as items - even if they affect the grand total. Every charge and saving is captured in `adjustments`.

The `adjustments` list holds every printed charge or saving that is not a purchased good: receipt-level charges (sales tax/VAT added on top of the items, a tip/gratuity/service charge, a delivery/booking/cover fee), whole-order discounts/vouchers (such as "£5 off £40 spend", a percentage off the whole bill, or a staff/loyalty discount taken off the order total), AND item-specific savings (Special Offer, multi-buy, Clubcard/CC) flagged with relates_to_item.

Each adjustment entry has:
- name: the label exactly as printed on the receipt
- translated_name: the English translation of the label (or the same value if already English)
- kind: one of "tax" (sales tax/VAT added on top of the items), "tip" (a tip, gratuity, or service charge - treat all three as the same kind), "discount" (ANY saving/reduction, whether a whole-order loyalty/membership/staff/military discount or coupon/voucher, OR an item-specific "Special Offer", promotion, multi-buy, or Clubcard/CC saving), "fee" (delivery/booking/cover charge), or "other"
- amount: a decimal string, NEGATIVE for subtractive adjustments (discounts, coupons, vouchers, savings) and POSITIVE for additive adjustments (tax, tip, service charge, fees)
- relates_to_item: true when the entry is a saving/charge tied to one specific item; false for genuinely receipt-level entries (tax, tip, fees, whole-order discounts)
- related_item_index: when relates_to_item is true, the 0-based index of that item in the `items` list; otherwise null

Rules for adjustments:
- Only include an adjustment when it clearly affects the grand total. Leave `adjustments` null if the receipt has none.
- Do NOT include VAT/sales tax that is already baked into the item line totals. Record tax as an adjustment only when it is added on top of the items to reach the grand total.
- Do NOT include cash-rounding lines (e.g. "Rounding -0.02") as adjustments.
- Do NOT include purchased goods or services here, and do NOT include these adjustments in items.

Do not duplicate savings summaries.

If the receipt shows both "Savings" and "Promotions" with the same amount, and they appear to refer to the same overall promotional saving total, do not output both.

If individual item savings have already been captured as their own adjustments, do not ALSO output the receipt's total savings/promotions summary as another adjustment. That would double-count the same saving.

Extract receipt_total as the final total charged on the receipt. If the receipt does not explicitly display a grand total, return null - do not calculate, sum, or otherwise invent a receipt_total from the items or charges.

Set receipt_total_visible to true when a grand total / amount due / total payable is clearly printed on the receipt, and false when no grand total is shown (or it is illegible or too ambiguous to read confidently). This reports only whether the printed total is legibly present - it is independent of whether the items add up to it. When receipt_total_visible is false, receipt_total must be null.

Extract receipt_establishment_name as the merchant or establishment name shown on the receipt if available.

Extract currency_code in ISO 4217 format, for example GBP, EUR, USD.
- Prefer an explicit currency symbol, code, or label printed on the receipt
- If no explicit currency is shown but the receipt's address (country/city), language, tax label (e.g. "VAT", "IVA", "MwSt", "GST"), or merchant clearly indicates a single dominant currency for that locale, set currency_code to that currency
- If the currency cannot be confidently determined from explicit markings or strong contextual evidence, return null - do not guess or invent a currency code from a weak signal

Report the honest set of items and savings even if they do not reconcile to receipt_total - do not adjust, add, or drop items or savings to force the totals to agree. The server computes the items total itself.

Extract datetime_of_receipt from the receipt date/time.
- Return it as an ISO 8601 string when possible
- If the receipt provides only a partial date or ambiguous date/time that cannot be confidently converted to ISO 8601, return null
- If no receipt date/time is present, return null

All monetary amounts (pre_discount_line_total, post_discount_line_total, price_per_quantity, receipt_total, adjustments.amount) must be returned as decimal strings normalized to US locale formatting:
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
- receipt_line_text: the raw text of the row(s) this item came from, verbatim; join multiple physical rows with " / ".
Items never carry a discount field - every saving goes into adjustments.

Discount / saving rows (e.g. "Multi-save -0.45", "Cc -1.15", "Special Offer -1.00", "Voucher -2.00"):
- These are NOT purchased goods, so do not output them as items.
- Transcribe every such row into adjustments as a separate entry: name = the label printed verbatim, translated_name = its English translation (or same), kind = "discount", amount = the printed amount as a negative decimal string.
- When the saving is printed directly under one item (or, for a multi-buy, under its group of qualifying rows), set relates_to_item=true and related_item_index to that item's 0-based position in `items` (the last qualifying row for a multi-buy). When it is a whole-order saving you cannot tie to a single item, set relates_to_item=false and related_item_index null. Do NOT do any arithmetic - the server folds the saving in.

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
