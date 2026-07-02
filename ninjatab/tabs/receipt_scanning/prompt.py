DOCUMENT_ANNOTATION_PROMPT = """
Extract structured data from this receipt.
Detect and extract the receipt language into receipt_language.
- If the receipt is in English, set receipt_language to "English".
- Otherwise set it to the detected language name.

Extract all purchased goods or services that contribute to the receipt total into items.
Do not include receipt-level charges such as tax, tip, service charge, or other fees/discounts in items - those are captured separately below.

CRITICAL - one output item per PRICED row. A priced row is a printed row that shows its own price. Every printed price in the items section of the receipt maps to at most ONE output item, and every output item maps to exactly one printed price.
- A printed row WITHOUT its own price is never an item of its own. Long item names often wrap onto a second printed line, and modifiers/options (e.g. "no ice", "extra cheese" with no price) are printed under their parent item: attach such rows to the adjacent priced row - include their text in that item's name and receipt_line_text - and do NOT emit a second item for them. Never copy the price of a neighboring row onto a row that has no printed price of its own; that would double the amount.
- Some layouts print the item name on one line and the quantity/price on the next line: that name line plus its price line together are ONE priced row and produce ONE item. Do not drop it and do not emit it twice.
- Never merge, combine, deduplicate, or consolidate multiple PRICED rows into a single output item, even if those rows have the identical name and identical price. If the receipt prints "Coke 3.00" on three separate priced rows, return three separate items each with quantity 1 and total 3.00 - do NOT return a single item with quantity 3 and total 9.00, and do NOT return a single item with quantity 1 and total 9.00. Only use quantity > 1 (and a correspondingly larger total) when a SINGLE priced row on the receipt itself shows an explicit quantity multiplier (e.g. "3 x Coke   9.00" on one line).
- The number of output items must equal the number of printed prices in the items section - not the number of printed lines.

For each item:
- name: the item name exactly as it appears on the receipt, in its original language
- translated_name: the English translation of the item name
  - Always attempt a translation when the item is not already in English, even if the original text is abbreviated, partially illegible, or you have to make your best guess from context (cuisine type, common menu items, surrounding items, the establishment name)
  - For abbreviated item names (e.g. "BIRRA DIAMOND GRAN", "ANT. PIEVE VECCHIA"), expand and translate the likely full meaning ("Diamond beer (large)", "Antipasto Pieve Vecchia")
  - Only fall back to copying the original name verbatim if you genuinely cannot make any reasonable guess at the English meaning
  - If the item is already in English, set translated_name equal to name
  - Be aggressive here: the precision/conservatism rules that apply to amounts, items, and dates do NOT apply to translated_name - always produce a best-guess English translation rather than leaving it untranslated
- quantity, price_per_quantity, total: see below
- adjustments: see below

Only include price_per_quantity and quantity if clearly on the receipt.
quantity: number of instances of this item purchased. Set to 1 if it is not clear
price_per_quantity: the price of this item per quantity
total: the printed price of this item's own priced row, so quantity * price_per_quantity.
receipt_line_text: the raw, verbatim text of the printed receipt row(s) this item was extracted from, exactly as it appears (including any item code, quantity, and price as printed). If the item spans multiple printed rows, join them with " / ". This is used to verify the extraction against the receipt - copy the source text faithfully, do not clean it up.

adjustments: when a discount, void, or price-change row clearly applies to ONE specific item (it is indented under it, refers to it by name, or is printed immediately after it), add it to that item's adjustments list - never output it as its own item and never as a receipt-level charge. Each adjustment has name (as printed), translated_name (English translation, or same value if already English), amount, and is_new_price. Two forms - distinguish them:
- Signed delta (is_new_price: false): the row adds or subtracts an amount from the item's price (e.g. "discount -1.20", "Happy hour 2.00 off"). amount carries an explicit sign: output a leading minus for reductions even when the receipt shows the reduction differently (in parentheses, or as "2.00 off").
- Replacement price (is_new_price: true): the row shows the price actually paid for the item, replacing the printed item price (e.g. "3 for 3.00", "cc price 5.50", a struck-through price with a new one below). amount is the new price, not a difference.
Keep the item's total as the printed price of the item's own priced row - do NOT compute the net price yourself; report both printed numbers faithfully.

Do not include subtotal, tax, VAT, tip, gratuity, service charge, payment method, change, balance, loyalty adjustments, or receipt-level discounts/fees as items - even if they affect the grand total. These are captured separately below.

Extract every receipt-level charge, fee, or discount into charges - one entry per printed charge row. Each entry has:
- name: as printed; translated_name: English translation (or same value if already English)
- kind: "tip" for tip or gratuity rows; "charge" for everything else - tax, VAT, IVA, MwSt, GST, sales tax, service charge, surcharges, delivery/booking/card fees, deposits, cash rounding, and receipt-level discounts, vouchers, or loyalty/promo reductions (use a negative amount for reductions - output the minus sign explicitly)
- amount: the printed amount, signed
- included_in_item_totals: whether this charge is ALREADY CONTAINED in the item prices, or ADDS ON TOP of them:
  - true: the charge is informational - the item prices already include it. Typical for European VAT shown as "VAT 20% included", "TVA incluse", "inkl. MwSt", "IVA inclusa", or a net/tax/gross breakdown table. Adding it to the item prices would exceed the printed total.
  - false: the charge is added to the item prices to reach the grand total. Typical for US sales tax, tips, service charges, delivery fees, and receipt-level discounts.
  - Arithmetic check: compare against the sum of the ITEM PRICES. If the item prices alone already sum to the printed grand total, any tax lines shown are included (true) - even when the receipt also prints a net/tax/gross breakdown where net + tax = total. If the item prices plus the charge amounts equal the printed grand total, those charges are NOT included (false).
Do not include subtotal, payment method, change, or balance rows in charges. Do not include line items in charges, and do not include charges in items.

Extract subtotal as the printed subtotal - the sum of the item prices before receipt-level charges - only if the receipt explicitly prints one (e.g. "Subtotal", "Zwischensumme", "Sous-total"). Do NOT use the net (tax-excluded) amount from a VAT/tax breakdown table as subtotal - on tax-inclusive receipts that net amount is smaller than the sum of the item prices and belongs to the tax table, not here. If no subtotal is printed, return null - never calculate it.

Extract receipt_total as the final total charged on the receipt. If the receipt does not explicitly display a grand total, return null - do not calculate, sum, or otherwise invent a receipt_total from the items or charges.

Extract receipt_establishment_name as the merchant or establishment name shown on the receipt if available.

Extract currency_code in ISO 4217 format, for example GBP, EUR, USD.
- Prefer an explicit currency symbol, code, or label printed on the receipt
- If no explicit currency is shown but the receipt's address (country/city), language, tax label (e.g. "VAT", "IVA", "MwSt", "GST"), or merchant clearly indicates a single dominant currency for that locale, set currency_code to that currency
- If the currency cannot be confidently determined from explicit markings or strong contextual evidence, return null - do not guess or invent a currency code from a weak signal

Calculate items_total as the sum of all item totals. Report the honest sum even if it does not match receipt_total - do not adjust, add, or drop items to force the totals to agree.

Extract datetime_of_receipt from the receipt date/time.
- Return it as an ISO 8601 string when possible
- If the receipt provides only a partial date or ambiguous date/time that cannot be confidently converted to ISO 8601, return null
- If no receipt date/time is present, return null

All monetary amounts (total, price_per_quantity, subtotal, receipt_total, items_total, charges.amount, adjustments.amount) must be returned as decimal strings normalized to US locale formatting:
- Use a dot (".") as the decimal separator
- Do not include any thousands separators (no commas, no spaces, no dots between groups of digits)
- Use the number of decimal places appropriate for the receipt's currency: 0 for currencies with no minor unit (e.g. JPY), 2 for most currencies (e.g. USD, EUR, GBP), 3 for currencies that use three decimals (e.g. JOD, KWD, BHD, OMR, TND). Match the precision shown on the receipt itself - never truncate "1.234" (a JOD amount) to "1.23"
- Use a leading minus sign for negative amounts (discounts)

Examples: "3.50" (USD), "1234.56" (EUR), "-1.20" (discount), "0.99" (GBP), "1500" (JPY), "12.345" (JOD).
Do not return values like "1,234.56", "1.234,56", "1 234,56", "20,00", or numbers with spurious extra decimal digits beyond the currency's precision, even if the receipt itself uses those formats. Convert from the receipt's local format to US format before returning.

Be precise and conservative about monetary amounts, quantities, dates, and which items contribute to the total. Do not invent prices or items that are not on the receipt. (Reminder: this conservatism does not apply to translated_name - see the translation guidance above.)
"""
