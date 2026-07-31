# Receipt ledger + v2 API — findings, decisions, plan

Working notes for the session that built the v2 scan endpoint. Everything here
is uncommitted work on disk; nothing is switched on in production.

**Evaluation corpus:** 68 labelled cases, 66 with captured OCR
(`labeler/ocr_captures/`, 5 runs x 3 calls each = 330 scans). Replays cost no
API calls. Session API spend: **3,737 of a 5,000 budget**.

---

## 1. What the change is

The pipeline reconciled a receipt as three roles (items, charges, discounts)
and then **flattened** them into one `items` list, because the shipped Flutter
client understands only claimable line items plus `isTax` / `isTip` booleans.
That flattening forced three hacks:

| hack | why it existed |
|---|---|
| `expand_discount_rows` — gross the item back up, emit the discount as a negative row | client has no concept of an adjustment |
| `demote_duplicate_taxes` — keep only the largest tax | `tax_selector.dart:52` deletes *every* `isTax` row on edit, so a second one is destructive |
| unrecognised fee becomes an ordinary "item" | a delivery fee had nowhere else to go |

`reconcile_ledger` now stops at the ledger. `flatten_for_v1` performs the
collapse, and only the v1 endpoint calls it.

```
POST /api/tabs/{id}/upload-receipt      -> flat list (unchanged contract)
POST /api/tabs/{id}/upload-receipt-v2   -> the ledger
```

### v2 shape

```jsonc
{
  "items": [                                  // only what someone ordered; all claimable
    {"name": "Crisps", "total": "2.00",       // net of its own discount
     "adjustments": [{"name": "3 for 2", "amount": "-1.00"}]},
    {"name": "Delivery", "total": "3.00"}     // fees are ordinary lines
  ],
  "adjustments": [                            // never claimable
    {"name": "Tax", "amount": "1.20", "type": "tax", "split": "proportional"}
  ],
  "items_total": 5.00, "adjustments_total": 1.20, "grand_total": 6.20,
  "receipt_total": "6.20", "totals_reconciled": true
}
```

---

## 2. What we tried

### 2.1 Ledger split — kept

Same OCR, same arithmetic, different bookkeeping. Verified on 250 scans that the
v1 output is materially unchanged: **24 differ, of which 18 are row order only**
(promoted charge rows moved to the end). The other 6 are consensus picking a
different candidate, because `select_best_line_items` now compares *ordered-item*
counts rather than item-plus-charge-row counts.

Supporting changes: `bill_total()` / `recompute_bill_totals()` in
`postprocess.py` so the consensus layer works on either shape;
`verify_and_repair` repairs against `receipt_total − adjustments` and only ever
drops item rows.

### 2.2 Model-side charge classification — REJECTED, twice

Added `type: Literal["tax","tip","service","fee","discount"]` to `_OtherCharge`.
Two independent 5-run captures, 990 API calls each:

| | baseline | + type, long prompt | + type, one-line prompt |
|---|---|---|---|
| p1_item_totals_f1 | **0.9575** | 0.9354 | 0.9418 |
| p1_item_count_exact | **0.9121** | 0.8667 | 0.8758 |
| p2_charges_f1 | **0.8020** | 0.7290 | 0.7619 |
| s_item_name | **0.9232** | 0.8632 | 0.8695 |
| rollup | **0.9106** | 0.8888 | 0.8957 |
| item rows / call | **8.21** | 8.84 | 8.87 |
| other_charges / call | 0.360 | 0.322 | 0.473 |

Paired per-case (baseline vs long prompt): rollup −0.0218 (−1.1 se, 20 cases
better / 41 worse — sign test p≈0.01); p1 −0.0221 (3 better / 12 worse);
s_item_name −0.0684 (−2.6 se).

**Conclusion: classifying pulls attention off transcribing.** Shortening the
prompt recovered only a third of the gap, so it is the field, not the wording.
Item rows inflated ~8% in both runs.

**Second finding, on charge inflation.** The long prompt kept an explicit
"having a type is not permission to list more charges" guard and charges went
*down* (0.360 → 0.322/call). The one-line version dropped that guard and charges
jumped to 0.473/call — while charge F1 still didn't beat baseline, so the extras
were wrong. **If this is ever retried, that guard is mandatory.**

Both reverted. `schema.py` carries a comment recording the cost so nobody
re-runs the experiment.

### 2.3 Keyword/lexical classification — REJECTED (earlier work)

- Broad lexical reclassification: rollup −0.0133, p2 −0.0503. Mis-tagged
  `deposit`/`pfand` on Estonian receipts, which were packaging items people
  actually bought.
- Narrower charge-word retag: p2 +0.0034 for p1 −0.0046. Rejected — item totals
  outrank charges.

The failure is structural: a keyword sees three words of OCR'd text in an
unpredictable language. This is why the code name-matches **tips only** (short,
distinctive vocabulary) and deliberately never name-matches tax.

### 2.4 Tax/tip-only scope — kept

`adjustments` now holds only tax, tip and receipt-level discounts. Delivery,
booking, card and handling charges become ordinary line items.

An intermediate version also demoted **service charges** to line items. That was
wrong and the numbers said so; see 2.6.

| | 5 types | service = line item | **service = tip** |
|---|---|---|---|
| p1_item_totals_f1 (v2) | 0.9575 | 0.9464 | 0.9529 |
| p1_item_count_exact | 0.9121 | 0.8394 | 0.8970 |
| p2_charges_f1 | 0.8020 | 0.5300 | 0.7030 |
| p3_self_reconciled | 0.8899 | 0.8899 | 0.8899 |
| rollup (v2) | 0.9106 | 0.8934 | 0.9045 |

Closure is unchanged to four decimals throughout — the money never moved, only
which list it was reported in. `p2` swings because it is scored against a label
taxonomy the design keeps changing relative to; treat it as unreliable until
4.1 lands.

### 2.5 Discount capture — measured, dataset insufficient

| | |
|---|---|
| labelled item-level adjustments | **3, across 2 of 66 receipts** (0 `is_new_price`) |
| labelled negative receipt-level charges | **0** |
| recall on those | **1.000**, all 5 runs, no variation |
| precision | 0.283 (misleading — see below) |

Inspecting all 7 "phantom" cases: `f71c4328` and `f52eb086` contain **real
discounts that reconcile**, which the labels recorded as net item totals rather
than adjustments; `1a271e05` is a 0.00 adjustment ("Add Hash Browns");
`96c17952` is a refund receipt with no `receipt_total`; the rest are single-run
artefacts.

So the labelling convention nets discounts into item totals, which means **the
dataset structurally cannot measure discount extraction**, and `p4` at ~0.29 is
largely measuring that mismatch.

One quality signal worth remembering: `f52eb086` returned a discount **named
"4.50"** — the model put a price in the name field. Rendering discount rows to
users will surface garbage names.

### 2.6 Service charge == tip — kept

Gratuity, tip and service charge are the same thing to a diner: money for the
staff, on top of what was ordered. Collapsing them raised p1, p1_count, p2 and
the rollup together (table above).

The change is safe because it is **pure provenance**. All four service charges
in the corpus arrived in the model's `service_charge` field — none via
`other_charges`, none via an item `kind`. So no name matching was needed, and
`_TIP_WORDS` deliberately does *not* gain "service charge": speculative lexical
matching is what regressed in 2.3.

The labels agree. Both service charges that have been typed are marked
`kind=tip, charge_type=service` (incl. the Turkish *10% Servis ücreti*).

After this change the pipeline emits exactly three adjustment types across 330
scans: **tax 48, tip 39, discount 26** (the 18 service charges joined the 21
tips; the 20 fees became line items). Zero charge rows left inside `items`.

---

## 3. Decisions

1. **`adjustments` holds tax, tip and receipt-level discounts. Nothing else.**
   Every other receipt-level charge is an ordinary line item, split evenly, and
   the user can switch that row to proportional.
   **A service charge is a tip** — gratuity, tip and service charge are
   interchangeable.
2. **No new classifier — not a keyword list, not a second model.** Tax and tip
   already come from the model's own field placement, and the question that
   matters (added on top vs already inside the prices) is arithmetic, not
   classification. Sections 2.2 and 2.3 are why.
3. **Item-level discounts stay netted into the item total** in v2, with the
   discount detail attached for display. A 3-for-2 on crisps belongs to whoever
   ate the crisps — it follows the item, so there is no split mode to get wrong.
   v1 still grosses up and emits a negative row, because that client needs it.
4. **Receipt-level discounts** (a bill-wide voucher) are adjustments: default
   proportional, user can switch to even.
5. **A discount is never claimable.** "Who's having the −£5 coupon?" is not a
   question the UI may ask.
6. **Scorer:** a labelled charge that is not tax or tip counts as an expected
   *line item*. Existing labels stay valid with no re-labelling.

### Accepted costs

- Roughly 20% of `other_charges` entries are taxes the model filed in the wrong
  place (65 of 318, measured on the rejected v3 capture — indicative only, that
  capture was degraded). Those become even-split line items: right money, wrong
  default split, one tap to fix.
- `p2_charges_f1` stays artificially low until the scorer change lands.

---

## 4. Plan of action

### 4.1 Backend — one item left

**Scorer change** (`labeler/evaluation/scorer.py`): move labelled charges that
are not tax or tip out of `expected_charges` and into `expected_items`.

Identifying them, from an inspection of all 27 labelled charges:

| signal | availability |
|---|---|
| `kind == "tip"` | set on 5, reliable (*Propina*, *20% Gratuity*, *12.5% Service Charge*) |
| `charge_type` | set on **1 of 27** — unusable |
| tax by name | 11 are plainly tax (*Tax*, *VAT 24.00%*, *PA State tax (6%)*, *Tax Total*) |

So tax is matched **by name, in the scorer only**. That is reading a human's
clean label text, not guessing from OCR output — a different proposition from
the pipeline name-matching that regressed in 2.3. Switch the scorer to prefer
`charge_type` once those 11 are typed.

**`charge_type_accuracy` needs the same mapping.** The labels use two orthogonal
axes (`kind` = charge|tip, `charge_type` = tax|service|fee); the pipeline uses
one. Since 2.6 collapsed service into tip, the metric reads 0.0000 for v2 — a
scorer artefact, not a pipeline failure. Map label to expected pipeline type:

| label | expect |
|---|---|
| `kind=tip` **or** `charge_type=service` | `tip` |
| `charge_type=tax` (or a tax-ish name) | `tax` |
| `charge_type=fee` | a line item, not an adjustment |

It carries weight 0, so it is not affecting the rollup.

### 4.2 App — all of the actual work, none of it started

- Render an `adjustments` list.
- Offer the proportional/even toggle on **any** row. Today `step_splits.dart:457`
  gates it behind `isTip || isTax`, so an ordinary line item cannot be made
  proportional — which decision 1 depends on.
- Read tax and tip from `adjustments` rather than from flagged item rows.
- Point the scan call at `/upload-receipt-v2`.

The backend is inert until these land.

### 4.3 Open questions

- **`charge_type` on the 27 labelled charges** is set on 4. Until more are
  typed, `charge_type_accuracy` measures almost nothing.
- **Discount labelling convention.** To ever measure discount extraction, a
  discounted item needs `total` = pre-discount price plus an `adjustments`
  entry, not a net total. Low priority: 2 receipts in 66.

---

## 5. Current numbers

66 cases x 5 runs = 330 scans, same OCR for both columns (post-processing only).

| metric | v1 (flat) | v2 (ledger) |
|---|---|---|
| p1_item_totals_f1 | 0.9494 | 0.9529 |
| p1_item_count_exact | 0.9091 | 0.8970 |
| p2_charges_f1 | 0.7030 | 0.7030 |
| p3_self_reconciled | 0.8899 | 0.8899 |
| p3_grand_total_correct | 0.8970 | 0.8970 |
| p3_receipt_total_correct | 0.9694 | 0.9694 |
| p4_adjustments_f1 | 0.2941 | 0.2941 |
| rollup | 0.9032 | 0.9045 |
| identical across 5 runs | 46/66 | 48/66 |
| failures | 0 | 0 |

**Read `p3_self_reconciled` as the honest one.** It is unchanged from the start
of the session, because the arithmetic was never touched. `p2` and `p4` are
measuring label conventions the design has deliberately moved away from, and are
not comparable across sections 2.4 and 2.1 until 4.1 lands.

```bash
# reproduce, no API calls
cd backend && source .venv/bin/activate
PIPELINES=v1:verified_consensus,strategy:verified_consensus \
  python manage.py shell -c "
from receipt_validation.replay import evaluate, format_report, load_captures
from labeler.evaluation.labels import load_labelled_cases
c, l = load_captures(), load_labelled_cases()
for p in ['v1:verified_consensus', 'strategy:verified_consensus']:
    print(format_report(evaluate(p, c, l)))"
```

---

## 6. Housekeeping done alongside

- Labeller data moved into the labeller: `labeler/ocr_captures/` (was
  `receipt_validation/labelled_runs_v2/`), `labeler/evaluation/labels.py` (was
  `labelled.py`), `labeler/evaluation/scorer.py` (was `labelled_scorer.py`).
  Neither module imports the backend; `receipt_validation/__init__.py` puts the
  repo root on `sys.path`.
- Deleted the stale fixture harness: `receipt_validation/cases/` (9 outdated
  `expected.json`), `scorer.py`, `report.py`, `results/`, the
  `validate_receipt_scan` command, and `RECEIPT_POSTPROCESSING.md`.
- `include_blocks` now threads through `ConcurrentConsensusStrategy` — without
  it, switching the labeller to a consensus strategy silently loses the
  bounding boxes.
- Labeller prefills from `verified_consensus` (3 calls/scan instead of 1), so
  the charges section arrives populated. Only **additive** charges prefill —
  inclusive VAT is excluded, matching the `charges: []` convention in existing
  labels. `charge_type` is deliberately left blank: prefilling it with the
  pipeline's own answer would make `charge_type_accuracy` agree with itself.
- Labeller case search: filename/uuid substring, establishment name, or exact
  numeric case id. Ignores the status tabs.
