# Receipt OCR Post-Processing Plan

Deterministic cleanups to apply to the Mistral OCR annotation in `ninjatab/tabs/receipt_service.py:scan_receipt` before returning to the client. Each step uses the raw OCR markdown (`response.pages[*].markdown`) and the structured `document_annotation` together, so the markdown must be threaded into the post-processing pipeline.

## Status legend
- ✅ Applied
- ⏳ Pending
- ⏭️ Deferred / not pursued

## Pipeline order

The pipeline runs after `_normalize_amounts_in_annotation` and before reconciliation:

1. Currency fallback warning bug fix
2. Row-level item explosion
3. Net-subtotal / payment / total blacklist on `other_charges`
4. VAT identity check
5. Zero-row policy (refined)
6. Post-reconciliation mismatch flag for UI

The remaining items (2 grand-total disambiguation, 5 currency from symbol, 7 multiplier-line detection, 8 date validation, 9 establishment cleanup) are deferred — none are needed by the failure modes seen in current sample receipts.

---

## Audit follow-ups (from end-to-end pipeline review)

### H5 ✅ Validate `currency_code` against supported `Currency` choices
The annotation `currency_code` was flowing through unchecked. The model could emit a non-ISO string ("EURO") or an ISO code we don't support ("NGN"); `get_decimal_places` would silently default to 2dp and the bad code would propagate into bill creation.

**Applied** in `scan_receipt`: after JSON parse we upper-case `currency_code`, check membership in `SUPPORTED_CURRENCY_CODES` (built from `ninjatab.currencies.models.Currency`), and fall back to `tab.default_currency` with a distinct warning if empty or unsupported.

### H6 ✅ Stringify `total` in reconciliation-added items
`_candidate_additions` was emitting `total` as a `float`, while model-returned items use `total: str`. The mixed type leaked to the mobile client.

**Applied**: `_candidate_additions` now formats `total` with `f"{amount:.{dp}f}"` where `dp` is the receipt-currency precision. Output is consistent `str` throughout.

### H7 ⏭️ `_normalize_amount_str` mishandles negative 3-digit fractions
Skipped for now. Negative values like `"-1.234"` for 2dp currencies still pass through the thousands-heuristic and become `"-1234"`. Revisit if logs show this firing.

### OBS ✅ Rich PostHog telemetry for receipt scanning
`scan_receipt` now accumulates a `_scan_metrics` dict during processing; `api.upload_receipt` strips it from the response and emits it as PostHog properties.

**Events:**
- `receipt_scanned` — fired on every successful scan with full property payload.
- `receipt_currency_fallback` — fired when `currency_source ∈ {fallback_missing, fallback_unsupported}`.
- `receipt_totals_mismatch` — fired when `items_total != receipt_total` (within tolerance).
- `receipt_scan_failed` — now includes `exception_type` and (when ocr returned empty) the same metrics payload, so failure rows are filterable.

**Properties on every event:**
- `tab_id`, `tab_default_currency`
- `annotation_present`, `annotation_parse_error`
- `currency_source` (`model` / `fallback_missing` / `fallback_unsupported`), `currency_code`, `currency_decimals`, `currency_unsupported_raw` (only on unsupported fallback)
- `items_count`, `items_total`, `ai_items_total`, `receipt_total`
- `items_match_receipt_total` (bool, null if no receipt_total), `items_receipt_gap` (signed difference), `ai_vs_server_total_divergence`
- `has_tax`, `has_tip`, `has_service_charge`, `other_charges_count`
- `reconciliation_action` (`none` / `items_dropped` / `candidates_added`), `reconciliation_items_delta`
- `date_parsed`
- `ocr_pages`, `ocr_markdown_chars`, `mistral_call_ms`

This gives PostHog the ability to slice scan performance by currency, by mismatch rate, by reconciliation behaviour, by Mistral latency, etc., without any further code changes.

### H1 ⏳ Reconciliation can silently delete legitimate items
`_reconcile_items_with_total` drops items whose `translated_name` matches `NON_CONTRIBUTING_KEYWORDS` when `items_total > receipt_total`. But "cover charge", "service charge", and similar can be legitimate contributing line items. If dropping one happens to make totals match, we silently hide a real item.

**Plan:** preserve dropped items with a `removed_by_reconciliation=true` flag and surface in the UI, OR only drop when a corresponding `tax/tip/service_charge/other_charges` field with the same amount also exists (proves the model double-booked).

### H2 ⏳ Reconciliation can double-count
`_candidate_additions` doesn't check for existing items with the same amount before proposing additions. If the model puts a service charge in both `service_charge` AND as an item, reconciliation may add it again.

**Plan:** before adding a candidate, skip if any existing item has matching `total` within tolerance.

### H3 ⏳ Reconciliation picks the first matching subset, not the most likely
When multiple subsets satisfy the tolerance (`{tax}` and `{tip}` both equal the gap), iteration order — not semantics — decides.

**Plan:** prefer single-element combos; break ties by which field is non-null/most plausible (e.g. prefer `service_charge` over `tax` when item prices look gross); surface ambiguity to the UI if multiple matches.

### H4 ⏳ `tax` is unconditionally a candidate addition
On gross-priced receipts (VAT-inclusive), adding `tax` double-taxes. On net-priced receipts (VAT-exclusive), adding `tax` is correct. We can't tell without parsing OCR.

**Plan:** detect VAT-inclusive vs VAT-exclusive from raw OCR keywords ("incl.", "with VAT", "TTC", "MwSt enthalten", "VAT included", "KÄIBEMAKSUGA"). Only include `tax` as a candidate when receipt is VAT-exclusive.

### M1 ⏳ `increment_scan_count` runs even on empty annotations
`api.py:610` increments after `scan_receipt` returns, regardless of whether annotation came back null. Users burn quota on failed scans.

**Plan:** only increment when annotation is non-null AND has ≥1 item.

### M2 ⏳ `check_scan_limit` + `increment_scan_count` is not atomic
Two concurrent uploads can both pass the check and both increment, crossing the budget by 1.

**Plan:** atomic `UPDATE ... WHERE receipt_scan_count < MAX` and check affected rows.

### M3 ⏳ No retry on transient Mistral failures
30s timeout or 5xx kills the scan; user re-uploads and pays again.

**Plan:** one retry on `mistralai` SDK errors / timeout with backoff.

### M4 ⏳ Full Mistral response logged at INFO including OCR markdown
`logger.info` at receipt_service.py:466 dumps the full markdown — receipt content, possibly card numbers, employee names, table numbers.

**Plan:** redact card-number-shaped digit runs (`\d{13,19}`); drop or sample the `pages[].markdown` field in logs; only log full response on error paths.

### M5 ⏳ Presigned S3 URL is logged
Same log line. URL is valid for 3600s — anyone with log access can fetch the image until it expires.

**Plan:** redact `image_url` from the logged response, and consider shortening expiry to ~120s (Mistral fetches immediately).

### M6 ⏳ `_normalize_amounts_in_annotation` bypasses `_annotation_decimals`
Line 224 uses `get_decimal_places` directly so the unknown-code warning never fires from this path, while line 286 does warn. Inconsistent.

**Plan:** route all currency-precision lookups through `_annotation_decimals` once the underlying warning condition (item 0 above) is fixed.

### M7 ⏳ `currency_code` capitalisation not normalised (partially handled by H5)
Pre-H5 we left the model's case as-is. H5 now upper-cases when valid; double-check no downstream code re-reads `currency_code` and re-lower-cases.

### M8 ⏳ No date sanity check
`datetime_of_receipt` from 2099 or 1899 is silently accepted.

**Plan:** clamp to `[today - 5 years, today + 1 day]`; fall back to today with a log if outside.

### M9 ⏳ Image extension defaults from filename, not content type
`upload_to_spaces` derives `ext` from `file.name.rsplit(".", 1)`. iOS uploads without an extension become `.jpg` regardless of actual content type.

**Plan:** derive from `content_type` (e.g. `image/png` → `.png`).

### M10 ⏳ `_collapse_redundant_translations` runs after reconciliation
Items added by `_candidate_additions` (with `name == translated_name`, e.g. "Tax") get collapsed and look indistinguishable from model-originals afterwards.

**Plan:** add `source: "reconciliation"` on items added by `_candidate_additions`; exclude them from the collapse step; optionally use the flag in the UI to render differently.

### L1 ⏳ No version pin discipline visible for `mistralai`
A breaking change in `mistralai.client.models.ImageURLChunk` import path silently breaks scanning.

**Plan:** pin to a known-good range in `requirements.txt`; add a smoke test that exercises the import + a single OCR call.

### L2 ⏳ `_s3_client()` instantiated per call
Module-level singleton would shave latency without changing semantics.

### L3 ⏳ Receipt images live forever in S3
Privacy concern.

**Plan:** S3 lifecycle rule to delete after N days post tab close (or N days absolute).

### L4 ⏳ Single failure-mode signal (timeout) — no telemetry on failure category
Can't distinguish bad JSON, bad image, timeout, 5xx.

**Plan:** Sentry-tag the failure category from `scan_receipt`.

### L5 ⏳ `MAX_SCANS_PER_TAB` is a tab-level cap
Abusive user on a shared tab locks legitimate users out.

**Plan:** per-user-per-tab cap.

### L6 ⏳ `safe_capture` reason hard-coded to "validation"
`api.py:606` reports `reason="validation"` for any `scan_receipt` exception, regardless of actual cause.

**Plan:** classify exception type and pass an accurate reason.

### A1 ⏳ Three conflicting policies on "is this charge a line item?"
The prompt routes tax/tip/service to dedicated fields. Reconciliation tries to add them back. `NON_CONTRIBUTING_KEYWORDS` tries to remove items that look like charges. These layers race.

**Plan:** simplify to a one-direction pipeline — augment items from charges only when `items_total < receipt_total`, never remove items. If `items_total > receipt_total`, surface "we found more than the receipt says" to the user rather than silently dropping.

### A2 ⏳ `ai_items_total` is captured but unused
Stored for forensics; no telemetry alert when it diverges from the recomputed sum.

**Plan:** Sentry-tag scans where `abs(ai_items_total - items_total) > tolerance`. Surfaces prompt regressions early.

### A3 ⏳ No checked-in regression corpus
`receipt_validation/cases/` is starting to form. Without checked-in raw OCR + expected annotation + expected post-processing per case, every prompt or pipeline change risks regression. The bugs above (H1, H2, H4) can only realistically be caught with a corpus.

**Plan:** finalise a fixture format, capture the receipts already discussed (MER, NIMETA #1/#2, PIEVE VECCHIA, PEPE VERDE, ESARS TAXI, PROFI CATERING, HAUL) with expected outputs, and run them in CI.

---

## 0. Bug fix: unknown-currency warning fires for EUR/GBP

`_annotation_decimals` warns whenever `currency_code` is not in `CURRENCY_DECIMAL_PLACES`, but that map only lists 0-dp and 3-dp exceptions. 2-dp currencies (EUR, GBP, USD, ...) fall through to the default and trigger spurious warnings every scan.

**Fix:** warn only when the code is non-empty AND fails a basic ISO 4217 shape check (3 alphabetic uppercase chars), OR maintain an explicit `KNOWN_ISO_4217` set. Prefer the shape check — cheap, future-proof.

---

## 1. Row-level item explosion

### What it does

For each annotation item with `quantity > 1`, scan the OCR markdown for printed rows whose name matches and whose printed price equals `price_per_quantity`. If exactly `quantity` such rows exist, replace the merged item with `quantity` separate items each with `quantity=1, total=price_per_quantity`.

### Why

The model sometimes ignores the prompt's "do not merge identical rows" rule and collapses N separate printed rows into one item with `quantity=N`. This is the dominant observed failure on multi-language receipts where the same item appears as several separate rows.

### Preconditions (must all hold to split)

- Model item has `quantity > 1`.
- Strict `OCR_count == model.quantity` — never `>=`. If we found 4 rows but the model said 5, do not fabricate a 5th.
- Name normalisation: case-fold, strip whitespace, strip currency symbols, decode HTML entities (`&amp;`), collapse repeated spaces. **No fuzzy matching** — Levenshtein could conflate two genuinely-different products.
- Price match exact to currency precision (apply `_normalize_amount_str` to OCR-extracted prices before comparing).
- Skip rows whose printed price equals `model.quantity * model.price_per_quantity` — those are summary lines, not item rows.

### Downsides / failure modes

- **False splits if OCR duplicated a row** (smudge, watermark). When the model also said qty=N by reading a multiplier column correctly, we'd split a single qty=N row into N rows. Total preserved, but cosmetic-only error. Guard via the "exclude summary lines" rule above.
- **Missed splits** when model and OCR disagree on the item name (umlaut drop, abbreviation expansion in `name`). Skip is the safe outcome — `items_total` still adds up; UI just shows one row instead of N. Acceptable.
- **UX trade-off**: N separate claims means N rows for the user to assign people to. This is the intended behaviour (it enables "A had 2, B had 4"), but worth knowing.
- **Robustness to OCR markdown variability**: Mistral emits both pipe-tables (`| name | qty | price |`) and plain lines (`JÄGERMEISTER €5.00`). The row-extractor must handle both and skip lines without a recognisable price.

### Implementation notes

- Log every explosion: `tab_id`, item name, qty before/after, matching OCR rows used.
- Sentry breadcrumb when OCR_count and model qty disagree even when we don't split — gives us forensic signal on prompt regressions.
- Hide behind a feature flag for the first week.
- Add a regression test per receipt currently in `receipt_validation/cases/`.

### Sample scan coverage

| Receipt | OCR_count vs model qty | Action |
|---|---|---|
| MER (Estonian, table format) | per-item 1==1 | no-op |
| NIMETA #1 (6 JÄGERMEISTER rows) | 6==6 | split ✓ |
| NIMETA #2 (already split) | model qty=1 | no-op |
| PROFI (Slovak, multiplier table) | per-item 1==1 | no-op |
| PEPE VERDE (misaligned table) | model has no qty>1 | no-op |
| ESARS TAXI (JOD) | 1 item | no-op |
| HAUL (zero-price items) | model has no qty>1 | no-op |

---

## 3. Net-subtotal / payment / total blacklist on `other_charges`

### What it does

After the model returns, strip entries from `other_charges` whose name matches a multilingual blacklist of:
- Net/gross subtotal labels (NET, NETO, MAKSUTA, IMPONIBILE, SUBTOTAL, ZWISCHENSUMME, HT, EXCL. VAT, KÄIBEMAKSUGA, ...)
- Grand total labels (TOTAL, KOKKU, TOTALE, GESAMT, СУМА, إجمالي, 合計, SPOLU, ...)
- Payment instruments (CASH, CARD, KAARDIMAKS, KORTTIMAKSU, BAR, ESPÈCES, ...)
- VAT-rate breakdown rows (`Sadzba A 23%`, `IVA 10%`, names containing only `%` plus a letter rate code)

### Why

Across multiple Estonian and Slovak receipts, the model has misclassified net-subtotal / gross-with-VAT / payment / VAT-rate-breakdown lines as line-item-affecting `other_charges`. The current `_reconcile_items_with_total` will happily try to add these back into items to make totals match — a latent double-counting bug.

### Risks

- False positive on an item literally named "Net" or "Total" — extremely rare on real receipts. Mitigation: require the row to also have no `total` in the items list with the same value, or restrict blacklist to exact-token matches (not substring).
- Maintaining a multilingual list is ongoing work. Start with the 6–8 languages seen in production logs (Estonian, Italian, Slovak, English, French, German, Spanish, Czech).

### Implementation notes

- Keep the blacklist as a Python dict keyed by category (`net_subtotal`, `grand_total`, `payment`, `vat_rate_breakdown`) so we can log which category caught each strip.
- Capture stripped entries in a `stripped_other_charges` field on the annotation (not returned to client) so we can audit.

---

## 4. VAT identity check

### What it does

If `tax` and `receipt_total` are both present, compute `expected_net = receipt_total - tax`. If any entry in `other_charges` (or any line that looked like a candidate addition) equals `expected_net`, that entry is the net subtotal — drop it.

### Why

Backstop for (3). Catches net-subtotal lines that slip past the keyword blacklist (label OCR'd badly, unknown language).

### Notes

- Run after (3) so the explicit blacklist takes priority.
- Tolerance: one minor unit of the receipt currency (already encoded in `_annotation_tolerance`).

---

## 6. Zero-row policy (refined)

### What it does

For each item with `total = 0`:
- If the model explicitly set `quantity = 0` → drop (genuine zero-quantity line, e.g. "out of stock" still on the bill).
- If `total = 0` but `quantity` is missing or ≥1 → keep, set a `needs_price` flag for the UI.

### Why

The HAUL receipt scan returned all items at 0.00 because OCR mangled the price column. Naively dropping zero-total items would erase a perfectly legitimate item list. The flag lets the UI prompt the user to fill prices.

### Risks

- Adds a new field to the annotation contract. Frontend must be updated to render `needs_price` (otherwise no regression — it's optional).

---

## 10. Post-reconciliation mismatch flag

### What it does

After all the above, if `items_total != receipt_total - (tax + tip + service_charge + Σ other_charges)`, set `items_match_total = false` on the annotation. Otherwise `true`.

### Why

Single signal the mobile UI can use to decide whether to surface a "totals don't match" warning to the user. Today the client would have to recompute this every render.

### Risks

None — it's a derived field; ignoring it is no worse than today.

---

## Cases the deterministic cleanups cannot fix

These need either a re-prompt with stricter table parsing, table-cell extraction from the markdown, or manual user review:

- **PEPE VERDE (Italian)**: severe table-column misalignment from Mistral; model fabricated `FETTUCCINE = 30.00` from misaligned context. `items_total` 95.50 vs `receipt_total` 65.50.
- **NIMETA**: a genuine ~5 EUR is missing from the OCR rows themselves — no recovery possible from the data we have.
- **PROFI CATERING**: model copied `Chlieb` (bread) onto an unnamed table row. Requires column-aware re-parsing.

For these, surface (10)'s `items_match_total = false` flag and let the user reconcile manually.

---

## Implementation order

1. Fix the EUR/GBP warning bug (immediate; logging noise).
2. Thread `raw_markdown` through `scan_receipt` into a new `_post_process_annotation(annotation, raw_markdown, tab)` helper.
3. (3) + (4): blacklist and VAT identity. Smallest blast radius; no schema change.
4. (1) Row explosion behind a feature flag. Add per-explosion logging and tests against `receipt_validation/cases/`.
5. (6) Zero-row refinement and (10) match flag. Requires a mobile-app contract update — coordinate with frontend.

## Observability

Every transformation should log at INFO with the same `tab_id` thread used by the existing `Mistral OCR response for tab` log line. Sentry breadcrumbs for divergences (OCR_count != model.quantity, items_match_total = false, blacklist strip) make it easy to spot prompt regressions without enabling debug logs.
