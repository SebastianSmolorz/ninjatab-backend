DOCUMENT_ANNOTATION_PROMPT = """
Extract structured data from this receipt.
Detect and extract the receipt language into receipt_language.
- If the receipt is in English, set receipt_language to "English".
- Otherwise set it to the detected language name.

Extract all purchased goods or services that contribute to the receipt total into items.
Do not include receipt-level charges such as tax, tip, service charge, or other fees/discounts in items - those are captured separately below.

CRITICAL - one output item per printed row. Never merge, combine, deduplicate, or consolidate multiple printed rows into a single output item, even if those rows have the identical name and identical price. If the receipt prints "Coke 3.00" on three separate rows, return three separate items each with quantity 1 and total 3.00 - do NOT return a single item with quantity 3 and total 9.00, and do NOT return a single item with quantity 1 and total 9.00. The output should have the same number of item rows as the receipt has printed item rows. Only use quantity > 1 (and a correspondingly larger total) when a SINGLE printed row on the receipt itself shows an explicit quantity multiplier (e.g. "3 x Coke   9.00" on one line).

For each item:
- name: the item name exactly as it appears on the receipt, in its original language
- translated_name: the English translation of the item name
  - Always attempt a translation when the item is not already in English, even if the original text is abbreviated, partially illegible, or you have to make your best guess from context (cuisine type, common menu items, surrounding items, the establishment name)
  - For abbreviated item names (e.g. "BIRRA DIAMOND GRAN", "ANT. PIEVE VECCHIA"), expand and translate the likely full meaning ("Diamond beer (large)", "Antipasto Pieve Vecchia")
  - Only fall back to copying the original name verbatim if you genuinely cannot make any reasonable guess at the English meaning
  - If the item is already in English, set translated_name equal to name
  - Be aggressive here: the precision/conservatism rules that apply to amounts, items, and dates do NOT apply to translated_name - always produce a best-guess English translation rather than leaving it untranslated
- quantity, price_per_quantity, total: see below

Only include price_per_quantity and quantity if clearly on the receipt.
quantity: number of instanced of this item purchased. Set to 1 if it is not clear
price_per_quantity: the price of this item per quantity
total: the final price paid for that line item so quantity * price_per_quantity.

Do not include subtotal, tax, VAT, tip, gratuity, service charge, payment method, change, balance, loyalty adjustments, discounts, or any other fees as items - even if they affect the grand total. These are captured separately below.

Extract receipt-level charges that affect the grand total into their dedicated fields:
- tax: total tax/VAT amount on the receipt, if shown
- tip: tip or gratuity amount, if shown
- service_charge: service charge amount, if shown
- other_charges: a list of any other receipt-level fees or discounts that affect the total but do not fit tax/tip/service_charge (for example: delivery fee, booking fee, cover charge, loyalty discount, voucher). Use a negative amount for discounts. Each entry should include name (as shown on the receipt), translated_name (English translation, or same value if already English), and amount.

Only populate these fields when the charge clearly affects the grand total. Leave them null if not present. Do not include line items in these fields, and do not include these charges in items.

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

All monetary amounts (total, price_per_quantity, receipt_total, items_total, tax, tip, service_charge, other_charges.amount) must be returned as decimal strings normalized to US locale formatting:
- Use a dot (".") as the decimal separator
- Do not include any thousands separators (no commas, no spaces, no dots between groups of digits)
- Use the number of decimal places appropriate for the receipt's currency: 0 for currencies with no minor unit (e.g. JPY), 2 for most currencies (e.g. USD, EUR, GBP), 3 for currencies that use three decimals (e.g. JOD, KWD, BHD, OMR, TND). Match the precision shown on the receipt itself - never truncate "1.234" (a JOD amount) to "1.23"
- Use a leading minus sign for negative amounts (discounts)

Examples: "3.50" (USD), "1234.56" (EUR), "-1.20" (discount), "0.99" (GBP), "1500" (JPY), "12.345" (JOD).
Do not return values like "1,234.56", "1.234,56", "1 234,56", "20,00", or numbers with spurious extra decimal digits beyond the currency's precision, even if the receipt itself uses those formats. Convert from the receipt's local format to US format before returning.

Be precise and conservative about monetary amounts, quantities, dates, and which items contribute to the total. Do not invent prices or items that are not on the receipt. (Reminder: this conservatism does not apply to translated_name - see the translation guidance above.)
"""
