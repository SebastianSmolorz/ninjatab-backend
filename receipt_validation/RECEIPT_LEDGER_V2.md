# Receipt ledger + v2 API — findings, decisions, plan

Working notes for the session that built the v2 scan endpoint. Everything here
is uncommitted work on disk; nothing is switched on in production.

**Evaluation corpus:** 72 labelled cases, all captured
(`labeler/ocr_captures/`, 5 runs x 3 calls each = 360 scans). Replays cost no
API calls. Session API spend: **4,091 of a 5,000 budget**.

**Where it stands: 9 scans in 10 produce a bill that adds up.**

| | |
|---|---|
| perfect on all 5 runs (adds up *and* every item exact) | **51 / 72  (71%)** |
| adds up on all 5 runs | 61 / 72  (85%) |
| fails at least once | 11 / 72 |
| identical answer across 5 runs | 50 / 72 |
| per-scan closure | 89.2% |
| per-scan perfect (adds up *and* every item exact) | 80.0% |
| per-scan item-total accuracy | 95.0% |

Read those three apart: **9 scans in 10 add up, 4 in 5 are fully correct.**
Closure is not correctness — a bill can balance on wrong rows.

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
  "items": [
    {"name": "Crisps",  "total":  "3.00", "category": "item"},     // grossed up
    {"name": "3 for 2", "total": "-1.00", "category": "discount"}, // sits under it
    {"name": "Delivery","total":  "3.00", "category": "item"},     // fees are lines
    {"name": "Voucher", "total": "-5.00", "category": "discount"}  // whole-bill, at the end
  ],
  "adjustments": [                            // tax and tip only; never claimable
    {"name": "Tax", "amount": "1.20", "type": "tax", "split": "proportional"}
  ],
  "items_total": 0.00, "adjustments_total": 1.20, "grand_total": 1.20,
  "receipt_total": "1.20", "totals_reconciled": true
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

### 2.7 Discounts as negative rows — kept, and it is what v1 already did

v1 has grossed items up and emitted a negative row beneath them all along
(`f71c4328`: item 29.95, `Discount -3.00`, item 27.95, `Discount -2.79`), so
moving `expand_discount_rows` into the shared path makes **v2 match v1 rather
than diverge from it**. The two now score identically.

| | before | after |
|---|---|---|
| p1_item_totals_f1 (v1) | 0.9494 | 0.9532 |
| p4_adjustments_f1 | 0.2941 | 0.3125 |
| rollup (v2) | 0.9045 | 0.9054 |
| p3_self_reconciled | 0.8899 | 0.8899 |
| identical across 5 runs (v2) | 48/66 | 46/66 |

Stability cost is real: v2 now inherits the run-to-run discount-row variation
v1 always had.

Two bugs the tests caught while making the change:

- **Money loss.** Discount rows were still promoted out of `items`, then dropped
  by the adjustments filter. Only tax and tip may leave the list now.
- **Double-stating.** A grossed-up row kept the `adjustments` entry it had just
  emitted as its own row. It now keeps only what was *not* expanded (zeros and
  `is_new_price` lines).

And one in the scorer: its ledger branch returned item rows verbatim, so the
grossed-up totals were compared against net labels. Both forms now use the same
"a credit reduces the line above it" collapse.

### 2.8 Deskew: a saturation guard — kept; adaptive thresholding — REJECTED

Four receipts were being rotated 15-16 degrees when they were already close to
straight. Three returned **exactly +16.10 deg**, the arithmetic maximum the
search can produce (`limit=15`, `step=1`, fine sweep to 16.0 in 0.1 steps).
That is not a detection: when the projection profile has no peak - a heavy
shadow binarised as text, say - the score climbs monotonically and the search
runs to the wall. A healthy image peaks inside the range and falls away both
sides.

**Kept:** refuse the answer when it lands within `SATURATION_MARGIN` (2 deg) of
the boundary. Touches only those 4 cases; the other 68 get byte-identical
images.

| corpus effect (5 runs on the changed cases, spliced into the full set) | before | after |
|---|---|---|
| p3_self_reconciled | 0.8899 | **0.9046** |
| p3_grand_total_correct | 0.8970 | **0.9121** |
| rollup | 0.9107 | **0.9162** |
| p1_item_totals_f1 | 0.9598 | 0.9581 |

`6f344b62` went 0.419 -> 1.000 (closure 0 -> 1). p1 dipped 0.0017, against the
standing "reject on any p1 regression" rule — kept anyway because closure
outranks item totals in `PRIORITY_WEIGHTS` (5.5 vs 5.0) and gained ten times
what p1 lost, well inside per-case noise (stdev ~0.026).

**Rejected: adaptive (local) thresholding.** Strictly better on shadowed
receipts — on one, global Otsu calls 24.4% of the image ink and reports
+14.9 deg on a straight receipt, where adaptive reads 4.0% and 0.0 deg. But
over the 30 cases whose angle it changed (2 runs each): item-total F1
0.9468 -> 0.9093, names 0.9654 -> 0.9023, **4 better and 9 worse**. It invents
small rotations on clean images (`b7bbbce1` 0.00 -> +1.30 deg, rollup
1.000 -> 0.177), and the peak-ratio gate paired with it suppressed real small
skews (`839db178` +1.00 -> 0.00, 0.993 -> 0.577). The cost is recorded in
`_binarize`'s docstring.

**Also added:** the labeller can now reject the deskew per case
(`Case.deskew_rejected`). It serves the raw photo, re-runs the scan without any
rotation (`scan_image --no-deskew`, which also turns off the strategy's own
deskew pass), and records the human judgement that the rotation made things
worse — the one signal the deskew cannot produce for itself.

**Known, untouched:** the labeller path deskews twice — `scan_image --deskew`
writes a straightened temp file, then the strategy deskews that again inside
`pre_process`. Harmless when the first pass is right; two chances to go wrong
when it is not.

### 2.9 Per-role tax/tip metrics — added

`p2_charges_f1` pools tax and tip into one multiset, so a tax read perfectly but
filed as a tip still scores 1.0: both amounts are present, the metric just does
not care which is which. `p2_tax_f1` and `p2_tip_f1` (`scorer.py`,
`result_amounts_by_role`) score the same amounts kept apart. That swap case is
exactly what the second unit test in `test_scorer_roles.py` pins.

Both carry **weight 0**, for two reasons: reweighting mid-comparison would
invalidate the baseline, and they are `p2_charges_f1`'s own amounts split in two,
so weighting them would count every tax and tip twice.

```
p2_charges_f1   0.8211   21 cases, 99 obs
p2_tax_f1       0.8833   12 cases, 60 obs   53 perfect,  7 zero
p2_tip_f1       0.8182   13 cases, 59 obs
```

The split immediately paid for itself: it isolated the tip failures below, which
were invisible inside the pooled figure.

### 2.10 `kind: "service"` on an item row is not a tip — kept

2.6 collapsed service charge into tip, which is right for the model's
`service_charge` **field**. It was wrong for `kind: "service"` on an item row.
That `kind` is the model's catch-all for "nobody ordered this row", and it lands
on cover charges, delivery, booking and card fees alike. An Italian *coperto* is
a per-head cover charge, not gratuity — `686f77f4` was sending €15 to the app's
tip control.

One line, `_KIND_TO_CATEGORY["service"]` from `"tip"` to `"item"`:

| | before | after |
|---|---|---|
| p2_tip_f1 | 0.7627 | **0.8182** |
| p2_charges_f1 | 0.7879 | **0.8211** |
| p1_item_count_exact | 0.8778 | 0.8861 |
| p1_item_totals_f1 | 0.9520 | 0.9498 |
| rollup | 0.9137 | 0.9141 |

The p1 drop breaks the standing "reject anything that lowers p1" rule, and is
accepted here because the per-case diff shows it is **one receipt**:

```
686f77f4 (coperto)    0.9350 -> 0.9750  +0.0400   the target
96c17952 (illegible)  0.2000 -> 0.0000  -0.2000   a £5.50 key-cutting slip
```

Nothing else moved. `96c17952` has no readable total and scores 0.2 either way.

The change is safe because it is provenance, not naming — no `"coperto"` string
was added anywhere. Corpus evidence:

```
service_charge field  : 90 obs across 6 cases   all still tips
item row kind=service : 73 obs across 4 cases   none is a labelled tip
```

The four `kind: "service"` receipts are the coperto plus OCR noise (*"Mülk"*,
*"CARD"/"TI"*, *"ΠΑ"* worth 0). Every genuine service charge in the corpus — all
six — arrived in the dedicated field, which still maps to tip.

### 2.11 A relative tolerance floor — REJECTED

`select_additive_charges` needs a subset to close the items→total gap within one
minor currency unit. `b667c6e8` misses by 90 IDR on a 2,139,700 bill, so every
charge is dropped including a perfectly-read 110,100 service charge. A relative
floor looked like the obvious fix. It is not.

Floors of 0.05% / 0.1% / 0.2% / 0.5%, applied to the subset search only, all give
the identical result: **p1 −0.0002, tip unchanged, rollup −0.0001.**

The trace shows why:

```
candidate items 1,170,000 -> unreconciled_none_added
candidate items 2,695,000 -> unreconciled_none_added
candidate items 1,835,000 -> subset_reconciled          the floor works
merged result   1,755,000 -> unreconciled_none_added    but this is what ships
```

The floor does fix the subset search. The candidate carrying the charges then
loses the consensus merge, which emits a different item set against which no
subset closes. Applying the floor to the shared `_annotation_tolerance` instead
is worse — it also loosens what counts as "reconciled", which is the signal
candidate selection ranks on, so it changes which candidate wins.

**The bottleneck is the consensus merge, not the tolerance.** Also worth noting:
`b667c6e8`'s own ground truth does not reconcile (`items 1,835,000 + charges
304,610 = 2,139,610` vs a printed `2,139,700`). The receipt rounds its own total
to the nearest 100 IDR and the human labeller transcribed that faithfully.

### 2.12 Where charge classification actually comes from

Traced every labelled tax and tip back to its location in the raw model output,
across all 15 calls per case:

```
field:tax             12   (12 of 12 taxes)
field:tip              5
field:service_charge   5   (10 of 11 tips)
ABSENT                 1
```

**22 of 23 land in a dedicated model field, on 15 of 15 calls.** Not one arrives
only as an item row or an `other_charges` entry needing a name to be guessed. The
23rd is `8732627e`, which the model *did* classify correctly and mis-transcribed.

So `_TIP_RE` in `categorise_charge` and `_categorize_item_name` **fire on nothing
that matters**. They are pure fallback carrying zero load today. The pipeline's
real jobs are merging `service_charge` into tip, deciding additive vs inclusive
arithmetically, and demoting duplicates — none of which is classification.

This is the quantitative backing for decision 2 (no new classifier). It also
explains 2.2's negative result: adding a `type` field cost 8% on item rows and
0.06 on names and bought nothing, because classification was already free and
correct. A second model — including a small fast one from another vendor — would
see strictly less than Mistral did (a name string, no layout), cannot beat 100%,
and fixes neither live failure.

### 2.13 `receipt_line_text` as a correctness signal — explored, not landed

Hypothesis: the verbatim line text is often more faithful than the structured
extraction on misaligned receipts. **Correct, and the signal is strong.**

All 9,013 extracted items have `receipt_line_text` populated. Testing whether the
extracted `total` appears as a number in the text it was copied from:

```
                    total IS in line text | total NOT in line text
extraction correct        7596            |       525
extraction wrong           174            |       718

precision  P(wrong | flagged)   57.8%   base rate 9.9%  -> 5.8x lift
recall     P(flagged | wrong)   80.5%
```

Three uses, measured:

**Direct repair — no.** Only 61 of 892 wrong items have exactly one alternative
number in their line text matching a label item. Swapping it in fixes 43, breaks
18. Net +25 rows in 9,013, plus a new way to corrupt correct data. The text is
honest about what was printed but not unambiguous about which number is the
total: 9.4% of *correct* items have some other number that also matches a real
item (quantity, unit price, per-kg rate).

**Candidate selection — real but marginal.** Scoring each candidate by the
fraction of rows agreeing with their own line text, ranked directly below
"reconciled":

| | before | after |
|---|---|---|
| p1_item_totals_f1 | 0.9498 | 0.9506 |
| p2_charges_f1 | 0.8211 | 0.8316 |
| p2_tax_f1 | 0.8833 | 0.9000 |
| s_item_name | 0.9189 | 0.9235 |
| rollup | 0.9141 | 0.9151 |
| identical across runs | 50/72 | **52/72** |

Placement is load-bearing: as the *top* term it overrides reconciliation and
costs −0.0091 rollup; as a final tiebreak it does nothing. Per-case it is 3 up
(`64415664` +0.067, `e7c009b5` +0.077, `fbca0105` +0.010) and 3 down
(`1a271e05` −0.007, `b8003ed3` −0.036, `8732627e` −0.050) — a wash with a small
net gain. Not enough to justify changing selection logic the whole baseline rests
on. Held.

**Row-level confidence in the UI — the one worth building.** 57.8% precision at
80.5% recall is weak for silently rewriting a number and strong for highlighting
one. The pipeline already ships receipts it knows do not reconcile; this tells it
*which row* to point at, which it currently cannot do. Flagged-wrong rows
concentrate heavily — `b7bbbce1` alone accounts for 361 of them, then `d1471a36`
113 and `e7c009b5` 86 — so most receipts would flag nothing.

---

## 3. Decisions

1. **`adjustments` holds tax, tip and receipt-level discounts. Nothing else.**
   Every other receipt-level charge is an ordinary line item, split evenly, and
   the user can switch that row to proportional.
   **A service charge is a tip** — gratuity, tip and service charge are
   interchangeable — but only when the model puts it in the `service_charge`
   *field*. `kind: "service"` on an item row is a catch-all, not a claim of
   gratuity, and stays a line item (2.10).
2. **No new classifier — not a keyword list, not a second model.** Tax and tip
   already come from the model's own field placement, and the question that
   matters (added on top vs already inside the prices) is arithmetic, not
   classification. Sections 2.2 and 2.3 are why.
3. **Discounts reach the app as negative line items**, not adjustments. An item
   discount grosses its item back up and sits directly beneath it; a whole-bill
   discount sits at the bottom. Both carry `category: "discount"`, which marks
   them non-claimable and proportional by default.
   *(This reverses an earlier "net them into the item" decision.)*
4. **The item-level vs whole-bill distinction is kept in the labels**, not in
   the app response: item discounts live in `items[].adjustments[]`, whole-bill
   ones in `charges[]` with `charge_type: "discount"` (added to the labeller).
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

### 4.1 Backend — DONE

**Scorer change** (`labeler/evaluation/scorer.py`) — landed. `label_charge_role`
maps the labels' two axes onto the pipeline's one, and labelled charges that are
not tax or tip are expected as ordinary rows.

Identifying them, from an inspection of all **29** labelled charges across 22 of
the 72 cases (**tax 12, tip 11, ordinary line item 6**):

| signal | availability |
|---|---|
| `kind == "tip"` | set on 6, reliable (*Propina*, *20% Gratuity*, *12.5% Service Charge*) |
| `charge_type` | set on **5 of 29** — 1 tax, 2 service, 2 fee |
| tax by name | 11 of the 12 taxes resolve only by name (*Tax*, *VAT 24.00%*, *PA State tax (6%)*, *Tax Total*) |

Both role metrics lean heavily on that name regex: only 1 of 12 taxes and 2 of
11 tips are explicitly typed. Five of the tips reach `tip` via `kind`, the rest
via the name.

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

A whole-bill discount is also no longer collapsed into the row above it:
`_is_whole_bill_row` tells the two apart by `charge_source`, which a discount
built by expanding an item does not have. Before this, a correct voucher scored
p1 = 0.0.

Effect of the whole change:

| | before | after |
|---|---|---|
| p1_item_totals_f1 | 0.9532 | 0.9598 |
| p2_charges_f1 | 0.7030 | 0.7640 |
| charge_type_accuracy | n/a | 0.5000 |
| rollup (v2) | 0.9054 | 0.9107 |

The 27 labelled charges read as **tax 11, tip 10, item 6** at that point (the
66-case corpus). Current figures are in 4.1's table above.

### 4.2 App — all of the actual work, none of it started

- Render an `adjustments` list.
- Offer the proportional/even toggle on **any** row. Today `step_splits.dart:457`
  gates it behind `isTip || isTax`, so an ordinary line item cannot be made
  proportional — which decision 1 depends on.
- Read tax and tip from `adjustments` rather than from flagged item rows.
- Point the scan call at `/upload-receipt-v2`.

The backend is inert until these land.

### 4.3 Open questions

- **`charge_type` is set on 5 of the 29 labelled charges.** Until more are
  typed, `charge_type_accuracy` measures almost nothing — it currently reads
  0.7500 over those five. This is also the gate on 2.12: a wider taxonomy (and
  therefore any argument for a classifier) cannot be evaluated until there are
  a few dozen typed charges.
- **Discount labelling convention.** To ever measure discount extraction, a
  discounted item needs `total` = pre-discount price plus an `adjustments`
  entry, not a net total. Low priority: 2 receipts in 66.
- **Two labels worth correcting.** `b667c6e8`'s *PBI* (194,510) is Indonesian
  PB1 restaurant tax — exactly 10% of items + service charge — but is untyped,
  so it reads as an ordinary line and `p2_tax_f1` never sees it. `686f77f4`'s
  *N. 5 COPERTO* reads as a line item only because nobody typed it and the name
  regex misses it; typing it `fee` would make the agreement recorded rather
  than coincidental.

---

### 4.4 The two remaining tip failures

Both are downstream of a **correct** classification. Neither is a classification
problem, and neither is fixed by a classifier (2.12).

**`b667c6e8` — the consensus merge, and the higher-value fix.** The model read
the service charge as 110,100, exactly right, on all 15 calls. One candidate
reconciles and carries it; the merge then emits a different item set (1,755,000
vs 1,835,000) against which no subset closes, and the charge is dropped. A merge
that preferred the candidate whose charges reconcile — rather than building a
fresh item set that reconciles against nothing — recovers a flawless extraction
in code, with no API call. See 2.11 for the trace.

**`8732627e` — one misread digit.**

`8732627e`: the model read the service charge as **758.50**; the receipt says
**756.50**. `select_additive_charges` tried `7565.00 + 758.50 = 8323.50` against
a receipt total of `8321.50`, missed by 2.00, concluded the charge was not
additive and **dropped it entirely**. The bill is now short 756.50 and fails
closure.

So one misread digit on a charge does not degrade that charge, it deletes it.
Including it with a 2.00 discrepancy would leave a more useful bill. Worth
weighing against the reason charges are dropped today (a charge that cannot be
shown additive may be inclusive VAT, and adding it double-counts).

## 5. Current numbers

72 cases x 5 runs = 360 scans, same OCR for both columns (post-processing only).

| metric | v1 (flat) | v2 (ledger) |
|---|---|---|
| p1_item_totals_f1 | 0.9498 | 0.9498 |
| p1_item_count_exact | 0.8861 | 0.8861 |
| p2_charges_f1 | 0.8211 | 0.8211 |
| p2_tax_f1 | 0.8833 | 0.8833 |
| p2_tip_f1 | 0.8182 | 0.8182 |
| charge_type_accuracy | 0.7500 | 0.7500 |
| p3_self_reconciled | 0.8992 | 0.8992 |
| p3_grand_total_correct | 0.9056 | 0.9056 |
| p3_receipt_total_correct | 0.9720 | 0.9720 |
| p4_adjustments_f1 | 0.2713 | 0.2713 |
| rollup | 0.9134 | 0.9141 |
| identical across 5 runs | 50/72 | 50/72 |
| failures | 0 | 0 |

Includes 2.9 (per-role metrics) and 2.10 (the `kind: "service"` demotion). 2.11
and 2.13 are not in these figures — neither landed.

The six most recently labelled receipts all close on every run and average a
0.9473 rollup against 0.9107 for the older 66 - newer labels are cleaner, which
is worth remembering when comparing figures across corpus sizes.

`charge_type_accuracy` is 0.7500 over the 5 typed charges — too small a sample
to read as anything but a placeholder. `p4_adjustments_f1` remains unreliable for
the reason in 2.5, not because of this change.

`p2_tip_f1` at 0.8182 is 4 imperfect observations out of 59, all in the two
receipts in 4.4. `p2_tax_f1` at 0.8833 is 7 zeros out of 60.

The two forms now differ only in where tax and tip sit and in one category name
(`discount` vs the old client's `service`), so they score the same on
everything the metrics look at. Across 330 scans the pipeline emits exactly two
adjustment types — **tax 48, tip 39** — and 40 discount rows inside `items`.

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
- `p2_tax_f1` / `p2_tip_f1` added to the scorer and to `replay.METRIC_KEYS`,
  with `test_scorer_roles.py` (3 checks) pinning the swap case, the v1/v2
  equivalence and the not-scored-when-absent convention. 102 tests pass.
- `test_ledger.py::test_a_service_charge_is_a_tip` renamed and inverted to
  `test_a_service_kind_row_is_an_ordinary_line` for 2.10. The four tests
  covering the `service_charge` *field* are untouched.
