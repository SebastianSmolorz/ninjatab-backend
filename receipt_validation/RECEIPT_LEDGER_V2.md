# Receipt ledger + v2 API — findings, decisions, plan

Working notes for the session that built the v2 scan endpoint. Everything here
is uncommitted work on disk; nothing is switched on in production.

**Evaluation corpus:** 111 labelled cases, all captured
(`labeler/ocr_captures/`, 5 runs x 3 calls each = 555 scans). Replays cost no
API calls. Session API spend: **6,261 of a 7,000 budget**.

> The corpus grew 66 -> 72 -> 86 -> 111 during this session. **Figures are not
> comparable across those sizes**, and section 2 tables state the size they were
> measured at. The 14 cases added third were deliberately chosen failure
> examples (supermarket loyalty discounts, a Colombian bar bill) and every
> headline number fell when they landed. The 25 added last are ordinary
> receipts, and they land almost exactly on the established item-total rate:
>
> | | new 25 | established 86 |
> |---|---|---|
> | p1_item_totals_f1 | 0.9442 | 0.9432 |
> | p3_self_reconciled | 0.9360 | 0.8829 |
> | p2_charges_f1 | 0.8800 | 0.7091 |
> | identical across runs | 17/25 | 66/86 |
>
> Item accuracy matching to three decimals across two independently gathered
> batches is the best evidence yet that ~0.944 is the pipeline's real rate.
> Closure and charges are much better on the new batch, which confirms the
> older 86 are skewed by deliberately-collected failures. The new cases are
> *less* stable, though — more receipts in unfamiliar scripts (Slovak, Arabic,
> Turkish, Czech) vary run to run even where the answer is usually right.

**Where it stands (111 cases):**

| | |
|---|---|
| perfect on all 5 runs (adds up *and* every item exact) | **83 / 111  (75%)** |
| adds up on all 5 runs | 94 / 111  (85%) |
| identical answer across 5 runs | 84 / 111 |
| per-scan closure | 89.0% |
| per-scan perfect (adds up *and* every item exact) | 80.0% |
| per-scan item-total accuracy | 94.7% |

Includes the three changes landed in 2.22 and 2.23 (un-multiply, transcription-ranked
candidate selection, savings-footer repair), worth +0.0174 item totals and
+0.0164 closure over the same corpus.

Read those three apart: **9 scans in 10 add up, 4 in 5 are fully correct.**
Closure is not correctness — and 2.14 is the proof: a receipt can balance to the
penny on twelve wrong rows.

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

> **Superseded by 7.9 (2026-08-09).** The conclusion below — that the labelling
> convention nets discounts into item totals, so the dataset cannot measure
> discount extraction — was true at 66 cases and is no longer. Labels now record
> the item **gross** with its discount nested in `adjustments`, attached by hand
> in the labeller UI, across 8 cases and ~35 discounts. That is ground truth for
> *attachment*, and 7.9 scores against it.

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

### 2.14 The gross-up bug — found by inspection, invisible to every metric

Three receipts, all supermarket loyalty formats, show the same failure. The
model returns an item total it **computed** rather than transcribed:

```
receipt (Sainsbury's)             pipeline
CUSHEL G/TOILET   13.25           13.25    <- the -4.00 saving was DROPPED
  Nectar Saving   -4.00            (gone)
J5 BLUEBERRY MUF   2.00            3.00    <- invented (2.00 + 1.00)
  Nectar Saving   -1.00           -1.00
FTD BRID BURG BUN  1.50            2.00    <- invented (1.50 + 0.50)
  Nectar Saving   -0.50           -0.50
YOUR SAVINGS TODAY:
  PROMOTIONS       5.50           -5.50    <- a summary line, taken as a real discount
BALANCE DUE       13.35           13.35
```

**Three errors that cancel exactly.** `+4.00` (dropped saving) `+1.00 +0.50`
(gross-ups) `-5.50` (phantom summary) sums to zero, so `totals_reconciled` is
true and the bill lands on the correct £13.35 through wrong rows. `£3.00`
appears nowhere on the receipt.

`IMG-20260328-WA0027` (Tesco Clubcard) is the same thing at scale — **six**
items grossed up, each by exactly its own discount, and a `Savings -£5.87`
summary that cancels the lot to the penny.

**The metrics never saw this.** WA0027 scores `p1 = 1.0000`, because the scorer
collapses a discount row back into the item above it and nets out to the same
figure the label records. The damage is real but it lands in the app's
per-person split, not in anything p1 measures: whoever claims the pigs in
blankets is billed £8.00 for a £5.00 item, and £5.87 of discounts is spread
proportionally across the whole table.

`IMG-20260328-WA0026` is a *different* bug worth keeping apart — `4 BIRRA IPA
$ 32.000,00` became 128,000 because the model multiplied a printed line total by
the quantity. It fails loudly (`reconciled: False`), so the arithmetic already
catches it.

### 2.15 Transcribed vs computed — the sharpest signal in the corpus

Classifying every item total by *how* it relates to its own `receipt_line_text`
(86 cases):

```
                                       n    wrong   rate
verbatim (appears in the text)      2793      57    2.0%
qty x price                          136      12    8.8%   (line prints no total)
qty x price, line HAS a total         48       8   16.7%
not verbatim, not qty x price        120     109   90.8%
ALL                                 3097     186    6.0%   <- base rate
```

**A total the model worked out is wrong 9 times in 10; one it copied is wrong
2% of the time.** Flagging the last bucket gives 90.8% precision at 58.6%
recall — a 15x lift, and it got *better* as the corpus grew (85.2% at 72 cases).

Distinguishing *computed* from merely *absent* is what makes this work. 2.13's
untyped flag also condemned `qty x price`, which is legitimate multiplication,
and scored only 57.8% precision.

**Candidate selection on this signal is the one intervention that pays.** Rank
the three consensus candidates by the share of their rows that are transcribed
rather than computed, slotted directly below "reconciled" (72-case corpus):

| | before | after |
|---|---|---|
| p1_item_totals_f1 | 0.9498 | 0.9572 |
| s_item_name | 0.9189 | 0.9229 |
| rollup | 0.9141 | 0.9172 |
| identical across runs | 50/72 | 54/72 |
| p4_adjustments_f1 | 0.2713 | 0.2439 |

Per case 5 better, 1 worse. Placement is load-bearing: above "reconciled" it
overrides the arithmetic and costs -0.0091 rollup; as a final tiebreak it does
nothing. **Not yet re-measured on the 86-case corpus, and not landed.**

### 2.16 Repairing a flagged row from its line text — REJECTED

The obvious follow-on: if the total is not in the line text, replace it with a
value that is. Four policies, all 3,097 rows:

```
policy                                             overrode  fixed  broke   net
A  largest number in line                              203      9    107    -98
B  largest money-shaped                                 99     11     31    -20
C  largest money-shaped, skip x/@/each lines             69      9     21    -12
D  largest money-shaped, skip when qty*ppq explains it   59      6      3     +3
```

Only D is positive, by 3 rows in 3,097, and D is the one that keeps the
`qty x price` exemption. Product names carry bigger numbers than prices
(`5.0m`, `210g`, pack sizes); money-shaping filters those but not unit prices,
which is exactly what must not be taken when quantity > 1.

**The finding that kills it:** `receipt_line_text` is model output too, and it
degrades on the runs where the total is wrong. The same Tesco row across two
runs:

```
run A:  '... 210g £6.50 / £3.25 each / Cc Any 2 For £5 -£1.50'
run B:  '... 210g        £3.25 each / Cc Any 2 For £5 -£1.50'
```

When the model grosses up it often also drops the printed total from the text it
reports — so on the runs that most need correcting there is nothing correct to
reach for. That is why *selecting a better candidate* works and *repairing a bad
one* does not: the replacement has to come from a call that transcribed, not
from a heuristic.

Second problem: overriding item totals **breaks closure**. Correcting the two
Sainsbury's gross-ups takes items to 11.85 against a 13.35 total, because the
dropped CUSHEL discount is still missing. `p3_self_reconciled` carries the
highest weight in the rollup, so this reads as a large regression.

### 2.17 Trusting `qty x price` only when no total was printed — REJECTED

Meant to catch WA0026 without condemning legitimate multiplication:

```
CURRENT   flags 120 items, 109 wrong -> precision 90.8%  recall 58.6%
REFINED   flags 168 items, 117 wrong -> precision 69.6%  recall 62.9%
```

21 points of precision for 4.3 of recall. The test cannot separate a printed
**unit price** (multiply — correct) from a printed **line total** (do not
multiply — WA0026's bug); both are money-shaped. `2 x £4.99 -> 9.98` is right
and gets flagged. WA0026 already ships `reconciled: False` and needs no help.

### 2.18 Redefining `total` as the printed figure — REJECTED (attempt 1)

The fix 2.14 argues for: stop asking the model to net a discount, since that
requires arithmetic and 2.15 shows arithmetic is where it fails. Prompt changed
to *"the line total for this row exactly as printed - copy the printed figure,
do not work it out"* plus a worked Buns/Saving example; `expand_discount_rows`
stopped grossing rows up, since the total now arrives gross.

Measured on 86 cases x 2 runs, against the same 2 runs of the old prompt:

```
p1_item_totals_f1        0.9272 -> 0.9269   -0.0002
p1_item_count_exact      0.8895 -> 0.8837   -0.0058
p3_self_reconciled       0.8480 -> 0.8571   +0.0092
p2_charges_f1            0.7045 -> 0.7273   +0.0227
p2_tax_f1                0.7500 -> 0.7857   +0.0357
s_item_name              0.9127 -> 0.8710   -0.0417
s_translated_name        0.8615 -> 0.8277   -0.0338
s_date_match             0.9753 -> 0.9383   -0.0370
rollup                   0.8772 -> 0.8770   -0.0002
identical across runs      70/86 -> 73/86
```

**It failed on its own terms.** The point was to stop the model computing
totals; computed totals *tripled*:

```
             verbatim   qty x price   COMPUTED
OLD prompt     91.8%       6.4%        1.8%   (22 rows)
NEW prompt     90.5%       4.3%        5.2%   (66 rows)
```

The targeted receipts did improve — Sainsbury's `PXL_20260604` 0.3968 -> 0.5714,
`b8003ed3` 0.6143 -> 0.6581, and 10 cases better against 6 worse — so the idea
is not wrong. The execution was: ~90 words of emphatic instruction that names
both wrong answers (*"do not add the saving back on to make 2.00, and do not
subtract it to make 1.00"*). Negative examples make the named behaviour salient.

This is the **third** time added prompt text has cost secondary extraction
(see 2.2). Names -0.042, dates -0.037, currency -0.023.

Reverted. Captures kept at `labeler/ocr_captures_verbatim/` (522 calls).

### 2.19 Same idea, positive-only phrasing — REJECTED, and worse

2.18's failure looked like a phrasing problem, so attempt 2 was *shorter than
the original line*, positive only, naming no wrong answer:

```
total: the line total as printed on that row of the receipt. Copy the printed
       figure; it should appear in receipt_line_text.
```

plus eight words on the adjustments bullet (*"Leave total as the figure printed
on the item's own line and we will apply the saving"*). Same
`expand_discount_rows` change.

It went further the wrong way:

```
             verbatim   qty x price   COMPUTED
OLD prompt     91.8%       6.4%        1.8%   (22 rows)
attempt 1      90.5%       4.3%        5.2%   (66 rows)
attempt 2      86.3%       5.8%        7.8%   (102 rows)
```

```
p1_item_totals_f1        0.9272 -> 0.9155   -0.0116
p3_self_reconciled       0.8480 -> 0.8323   -0.0156
p3_grand_total_correct   0.8488 -> 0.8140   -0.0349
p2_charges_f1            0.7045 -> 0.6818   -0.0227
s_item_name              0.9127 -> 0.8641   -0.0485
p4_adjustments_f1        0.3449 -> 0.4444   +0.0995
rollup                   0.8772 -> 0.8546   -0.0226
identical across runs      70/86 -> 75/86
```

**The hypothesis is refuted, not the wording.** Two independent phrasings, one
verbose and negative, one terse and positive, both drove computed totals *up* —
1.8% -> 5.2% -> 7.8%, monotone with how hard the instruction pushed. Asking for
"the printed figure" makes the model reach for *a* printed figure, and on a line
carrying a price, a unit price and a saving it picks wrong more often than the
old "final price paid" framing, which anchors on a concept it models well.

One real gain worth keeping in mind: **`p4_adjustments_f1` rose 0.3449 ->
0.4444**. Telling the model to leave `total` alone and hand us the saving did
get discounts attached to their items properly. The discount *capture* improved
while the totals got worse — so if the app ever needs item-level discounts as
first-class data, that half of the instruction is worth revisiting on its own.

Captures kept at `labeler/ocr_captures_verbatim2/` (528 calls).

### 2.20 Minimal +19-word edit — REJECTED, best of the three

Third attempt, designed against the previous two: keep the sentence shape, keep
the `quantity * price_per_quantity` anchor (2.19 lost it and did worse), add
only a qualifier, and re-use the adjustments clause that had *worked*.

```
total: the full price of that line as printed on the receipt, before any
       discount - so quantity * price_per_quantity.
...give the delta (e.g. "-0.45"); total stays at the pre-discount price and we
   subtract it ourselves.
```

```
             verbatim   qty x price   COMPUTED
BASELINE       91.8%       6.4%        1.8%   (22 rows)
attempt 1      90.5%       4.3%        5.2%   (66)
attempt 2      86.3%       5.8%        7.8%   (102)
attempt 3      87.4%       7.6%        4.9%   (63)   <- best of the three, still 2.7x baseline
```

```
p1_item_totals_f1        0.9272 -> 0.9261   -0.0011
p1_item_count_exact      0.8895 -> 0.9070   +0.0174
p3_self_reconciled       0.8480 -> 0.8274   -0.0206
p3_grand_total_correct   0.8488 -> 0.8140   -0.0349
p3_receipt_total_correct 0.9766 -> 0.9940   +0.0174
p2_charges_f1            0.7045 -> 0.7273   +0.0227
p4_adjustments_f1        0.3449 -> 0.4667   +0.1217
s_item_name              0.9127 -> 0.8635   -0.0492
rollup                   0.8772 -> 0.8594   -0.0178
identical across runs      70/86 -> 78/86   (stdev 0.0170 -> 0.0035)
```

Item totals essentially held (-0.0011) and row *counts* improved, but closure
fell again and names took the same ~0.05 hit all three attempts took.

**Three attempts, one conclusion.** Verbose+negative, terse+positive, and
minimal+anchored all raised computed totals above baseline and all lowered
closure. The field definition is not the lever, and the cost is remarkably
consistent: **every version of this change cost ~0.04-0.05 on `s_item_name`**,
which is the 2.2 tax on adding prompt text, now observed five times.

**Two effects worth separating out, both reproducible across attempts:**

- **`p4_adjustments_f1` rose every time** — 0.3449 -> 0.4444 -> 0.4667. The
  "hand us the saving" clause reliably gets discounts attached to their items
  instead of emitted as sibling rows. If item-level discounts ever become
  first-class app data, that clause is worth revisiting *on its own*, without
  touching `total`.
- **Run-to-run stability improved sharply** — 70/86 -> 78/86, rollup stdev
  0.0170 -> 0.0035. A tighter definition makes the model more self-consistent
  even while making it less correct.

Reverted; prompt byte-identical to the 5-run baseline. Captures at
`labeler/ocr_captures_v3/` (528 calls).

**Where this leaves 2.14.** The gross-up bug is real, systematic across UK
supermarket formats, and **not fixable by redefining the field** — that is now
settled across three independent phrasings and 1,578 API calls. The remaining
routes are the 2.15 candidate selector (free, +0.0074 on 72 cases, not yet
re-measured on 86) and surfacing the flag in the UI rather than correcting it.

**If it is retried**, do it on a corpus with more than six labelled
discount receipts — the signal is currently being judged on too few cases to
separate a real effect from prompt-length noise.

### 2.22 Un-multiply, and select on transcription — BOTH LANDED

The two things that finally worked. Neither touches the prompt or the schema;
both cost zero API calls.

**`unmultiply_line_totals`** (`ledger.py`). The model sometimes reads a printed
*line total* as a unit price and multiplies it out — `4 BIRRA IPA $ 32.000,00`
came back as 128,000 against a 137,000 bill. The tell is that the emitted total
is absent from `receipt_line_text` while `price_per_quantity` is present.

That tell alone is not actionable: `2 x £4.99 -> 9.98` is textually identical
and correct. **The receipt's own total is the arbiter** — the row is rewritten
only when doing so closes the items-vs-`receipt_total` gap. On WA0026 the items
overshoot by 96,000, exactly `(4-1) x 32,000`, which names the culprit.

```
NAIVE      (ppq printed, total not)      46 rows -> fixed 11, broke 28   net -17
ARITHMETIC (...and it closes the gap)     7 rows -> fixed  7, broke  0   net  +7
```

Runs before charge selection, so a corrected items sum also feeds the additive
search. Five tests cover the fix and the three ways it must decline.

**`transcribed_fraction` in `select_best_line_items`** (`postprocess.py`). Rank
the three consensus candidates by the share of rows whose total was read off the
receipt rather than derived, slotted directly below "reconciled".

```
430 scans
  candidates all equally transcribed (term is a no-op):  379/430 = 88%
  scans where it changed the outcome:                     12      = 2.8%
     improved 10, worsened 2
     mean gain on the improved   +0.346
     mean loss on the worsened   -0.162
```

Dormant on seven receipts in eight; decisive on the rest. Placement is
load-bearing: above "reconciled" it overrides the arithmetic and costs -0.0091
rollup; as a final tiebreak it does nothing. It cannot change *whether* a
candidate reconciles, only which of the equally-reconciling ones ships —
`p3_self_reconciled` is identical to four decimals with and without it.

Replicated across corpus sizes: **+0.0074 on 72 cases, +0.0073 on 86**, with the
corpus materially harder in between.

**Combined effect (86 cases x 5 runs):**

| | baseline | +unmultiply | +selector |
|---|---|---|---|
| p1_item_totals_f1 | 0.9295 | 0.9359 | **0.9432** |
| p1_item_count_exact | 0.8698 | 0.8698 | 0.8721 |
| p3_self_reconciled | 0.8665 | **0.8829** | 0.8829 |
| p3_grand_total_correct | 0.8721 | 0.8884 | 0.8884 |
| s_item_name | 0.9007 | 0.9034 | 0.9072 |
| rollup | 0.8874 | 0.8984 | **0.9014** |
| identical across runs | 63/86 | 62/86 | **66/86** |
| p4_adjustments_f1 | 0.3697 | 0.3697 | 0.3579 |

The only regression is `p4_adjustments_f1` -0.0118, the metric 2.5 established
cannot measure discounts. `IMG-20260328-WA0026` goes 0.5000 -> 1.0000; the
Sainsbury's receipt gains +0.0929 — the only movement on the 2.14 gross-up bug
achieved without a prompt change.

**Why these worked when nine other things did not.** Every rejected intervention
either added words to the prompt (2.2, 2.18, 2.19, 2.20 — each cost ~0.05 on
`s_item_name`) or guessed a replacement value from text (2.16, 2.17 — each broke
more rows than it fixed). These two do neither: one lets the receipt's own
arithmetic decide, the other picks between three answers already paid for.

### 2.23 The savings footer — LANDED, and it fixes 2.14

The gross-up bug, finally repaired — in post-processing, after three prompt
attempts failed at it.

**Why nothing had caught it.** A loyalty receipt prints each saving under its
item and totals them at the bottom (Tesco `Savings -5.87`, Sainsbury's
`PROMOTIONS 5.50`). The footer is a restatement, not a further deduction. The
model reports it as a receipt-level discount *and* inflates each item by the
saving it absorbed. `select_additive_charges` then finds that subtracting the
footer closes the bill — arithmetically true, substantively wrong. The receipt
reconciles to the penny on rows that are each incorrect, and every existing
check passes it.

**Detection is arithmetic, not lexical.** The footer is the negative charge
equal to the sum of the discount rows already in `items`:
`5.87 == 1.50+0.80+0.40+1.08+0.75+1.34`. No keyword list.

**Neither half can be undone alone.** Dropping the footer by itself broke
closure on **all 17** scans where it was detected, because the gross-ups add
exactly what it removes. The two errors are one error. So the repair reverses
both together and is accepted only if the bill still closes.

**At least two item-level discounts are required.** With one, a real discount
reported once as a row and once as a charge is indistinguishable from a footer;
`4bf96933` is exactly that shape and must not be touched.

| | before | after |
|---|---|---|
| p1_item_totals_f1 | 0.9434 | **0.9471** |
| p1_item_count_exact | 0.8685 | 0.8775 |
| s_item_name | 0.9068 | 0.9079 |
| p4_adjustments_f1 | 0.3152 | 0.3164 |
| rollup | 0.9084 | 0.9096 |
| identical across runs | 83/111 | 84/111 |
| p3_self_reconciled | 0.8949 | 0.8949 |

**`IMG-20260328-WA0027` goes 0.5931 -> 1.0000 on all five runs.** Closure is
unchanged by construction — the repair is gated on preserving it. Nothing
regressed; `4bf96933` correctly untouched at 0.9455.

**Scope is narrow: 4 scans of 555**, worth +0.0037. It needs the model to have
captured *every* individual saving plus the footer. Sainsbury's usually drops
one (the CUSHEL -4.00), so the sums disagree and the rule correctly declines
rather than half-repairing — `PXL_20260604` stays at 0.3532. The case for it is
not the corpus number but that the pattern is systematic across UK supermarket
loyalty schemes, which the corpus under-represents at 7% of receipts and real
users probably do not.

**A false start worth recording.** The first implementation ran after
`expand_discount_rows` and looked for the footer among the *item rows*. It never
fired: the footer arrives via `other_charges` and is only merged into `items`
after charge selection. It has to run before `select_additive_charges`, on the
charge list, which is also the only point where removing it can stop the search
accepting it.

### 2.24 Perspective correction — text-block dewarp REJECTED, YOLO segmentation REJECTED, but the gate that would make it work was found

The defect this chases is not rotation. A receipt cupped in a hand prints
straight rows onto curved photographed ones: on `PXL_20260801_115103443` (Tesco)
the text rows ran **-3.97 deg at the top and -0.61 deg at the bottom**. That is a
*gradient*, and no single angle removes it — the deskew of 2.8 correctly reports
0.00 deg on that photo. Across a 270px block, 3 deg is 14px of drift against a
12px line pitch, i.e. exactly one line, which is why the OCR paired every price
with the description above it and double-counted a £1.00 discount. 14 of 15 calls
made the identical error, so consensus cannot help either.

**Attempt 1: find the receipt from its own text. REJECTED.**

Paper edges were tried first and abandoned — held in a hand, the left edge is
white paper on a bright palm and the bottom edge is white paper on a white
carrier bag, so two of four boundaries carry no contrast. Canny contours, an HSV
paper mask and a Hough search all failed on the same photo.

Locating the receipt by its *text rows* (ink = dark pixels on a locally bright
background, joined with a 3x45 kernel, edges fitted to the row extremes) fixed
the target case outright: rollup 0.575 -> 0.991, p1 1.000, reconciles. But it
declined on 3 of the next 4 photos tried, for three unrelated reasons — a pub
receipt too dim for the `bg > 180` test, blue biro on dark leather with no
printed block to find, and a receipt photographed sideways whose vertical text a
horizontal row kernel cannot see. Too narrow to ship.

**Attempt 2: a YOLO segmentation model (Roboflow `receipt-segmentation-jzoro/1`).**
Returns the polygon of receipt pixels; the quad around it is warped square.

Fitting the quad: convex hull, `approxPolyDP` at rising tolerance until four
corners remain (min-area rect as fallback), then **grown about its centre until
every hull point is inside it**. Four corners cannot trace a curled receipt, and
the leftovers between quad and mask are always receipt.

*Cropping tighter was measured and rejected.* Over 20 supermarket/skewed cases,
a quad fitted without the growth step clips ~8% of the mask:

| | baseline | seg+deskew | seg | tight+deskew | tight |
|---|---|---|---|---|---|
| rollup | 0.841 | **0.853** | 0.779 | 0.729 | 0.813 |
| p1_item_totals_f1 | 0.849 | **0.862** | 0.820 | 0.798 | 0.785 |

Tight collapsed the Lidl case 0.938 -> 0.278. *Deskewing after the warp is
required*, though — the quad follows the mask, and a mask slightly off square
leaves the crop rotated (measured -1.30 deg on a receipt level in the
photograph). It is worth +0.074 rollup (0.779 -> 0.853): ASDA 0.400 -> 1.000,
Sainsbury's 0.190 -> 0.982, Sintra 0.938 -> 0.988, JIP 0.940 -> 0.997.

**Whole corpus, 126 cases, 2 runs each, against the captured baseline:**

| metric | baseline | seg+deskew | delta |
|---|---|---|---|
| rollup | 0.8938 | 0.8910 | **-0.0027** |
| p1_item_totals_f1 | 0.9295 | 0.9203 | -0.0092 |
| p1_item_count_exact | 0.8794 | 0.8651 | -0.0143 |
| p3_self_reconciled | 0.8758 | 0.8911 | +0.0153 |
| p4_adjustments_f1 | 0.3608 | 0.3294 | -0.0314 |
| s_date_match | 0.9565 | 0.9826 | +0.0261 |

**21 better, 22 worse, 83 unchanged.** 96 of 126 segmented; the 30 that fell back
to plain deskew *improved* (+0.0234) while the segmented ones did not (-0.0109) —
the only positive contribution came from cases segmentation declined to touch.

The mean hides a heavy tail both ways. Wins: `IMG-20260521-WA0019`
0.431 -> 0.999, `PXL_20260604_201408239` 0.441 -> 0.841, Tesco 0.575 -> 0.926.
Losses: **`50f2206b` 0.999 -> 0.084**, `IMG-20260521-WA0005` 0.994 -> 0.359,
`IMG-20260521-WA0003` 0.707 -> 0.293. Taking a perfectly-read receipt to 0.084 is
a worse failure than anything it fixes.

**REJECTED** at -0.0027 for ~600ms and a third-party dependency per scan.

**The gate that works, and is worth revisiting.** Two attempts at a geometric
gate failed outright. Aspect ratio: the +0.541 win sits at h/w 3.29, beside a
-0.513 regression at 3.44, and one long till roll fitted *landscape* at 0.30.
Quad fill: regressions at 63.8/77.4/80.2% overlap wins at 82.9/84.2%, and a
62.4% case is fine. With three regressions in nineteen cases, any threshold is
fitted to three points.

What does separate them is **disagreement between the 3 OCR calls within one
scan** — free, already computed, no geometry:

| | n | mean delta |
|---|---|---|
| calls disagree on item count | 40 | **+0.0329** |
| calls agree | 86 | -0.0193 |

Stable at every threshold tried (>0.0, >0.05, >0.15 all give ~+0.010). Split-half
validation — threshold fitted on a random half, scored on the other, 200
shuffles — gives a **held-out corpus delta of +0.0096 (median +0.0092), positive
in 177 of 200 splits**. It skips both catastrophic regressions (`50f2206b` and
`IMG-20260521-WA0005` show zero disagreement).

The reasoning is causal, not incidental: three independent reads disagreeing
about how many rows exist *is* the signature of hard geometry. Agreement means
the photo is already legible and rewarping can only add error.

Not landed, for two reasons. Disagreement is only observable *after* scanning, so
the flow is scan (3 calls) -> if they disagree, segment and scan again (3 more) —
double cost on ~32% of receipts. And +0.0096 is small against the 0.0273
run-to-run stdev: real in the paired comparison, imperceptible to a user. The
`tiered_consensus` strategy already has the escalate-on-uncertainty shape if this
is revisited.

**Two process notes.** Both attempts were recommended for rejection on a
7-image sample and both recommendations were wrong — post-warp deskew looked
like dead weight until a 20-case set showed it worth +0.074. Single-run deltas
on small sets are noise. And the visual signal misleads: the bakery receipt
`IMG-20260328-WA0005` was warped into an alarming shear, and scored **1.000 on
every path**.

### 2.21 Does "quantity * price_per_quantity" make the model invent quantities?

Tested for free, since attempt 2 removed the multiplication language entirely
and attempt 1 demoted it to a fallback — three prompts over identical images:

```
                                    ppq not in line   qty not in line   total = qty x INVENTED ppq
BASELINE  "so quantity * price"          12%               6%            142 rows,  2% wrong
ATTEMPT 1 "fall back to qty*price"       11%               7%            117 rows,  2% wrong
ATTEMPT 2 no multiplication language     13%               5%            146 rows,  3% wrong
```

**No.** Removing the phrase left an invented unit price slightly *more* common,
not less. And rows whose total is `quantity x an invented ppq` are **2% wrong
against a 6.0% base rate** — the safest rows in the corpus.

The direction of derivation is why: the model reads the total off the receipt
and back-fills a unit price to populate the field, rather than computing the
total from a formula. `IMG-20260328-WA0026` shows both directions on one bill —
`14 BIRRA RUBIA $ 105.000,00` gives ppq 7500 (invented, total correct) while
`4 BIRRA IPA $ 32.000,00` gives 128,000 (total invented, wrong). Same prompt
language produced both. The failure is mistaking a printed **line total** for a
printed **unit price**, which no amount of removing arithmetic wording touches.

---

### 2.25 Blob deskew replaces the projection search — LANDED

The projection profile (2.8) rotates the binarised image through candidate
angles and keeps the one whose row-sum variance is highest. It is slow, and on
a visibly skewed receipt it frequently reports 0.00.

**Landed:** `method="blobs"`, now the default in `detect_angle` / `deskew_image`
/ `deskew_bytes`. Characters are isolated by connected-component size, smeared
into text lines with a horizontal close whose width scales with the median
character height, and the median of the lines' `minAreaRect` angles is the skew.
`method="projection"` is kept and still selectable.

23ms against 81ms. Over the 221-image corpus: blocks per image median 29, and
**no image fails to find text at all** (39 did before the local-threshold
fallback and the paper crop).

Three guards, each forced by a real image:

| guard | the image that forced it |
|---|---|
| `MIN_VOTES = 15` | a long till roll on a wood floor: 11 grain blobs voted −10.6 deg on a straight receipt |
| character-size CC filter | wood grain, table edges and fingers all survive an aspect-ratio filter once smeared |
| paper crop + local threshold | Otsu splits paper from background, not ink from paper, when the receipt is small in a dark frame |

Scored by re-capturing the 59 labelled cases whose angle changed by >1 deg
(5 runs x 3 calls, 900 calls):

| metric (those 59 cases) | projection | blobs |
|---|---|---|
| p2_charges_f1 | 0.6182 | **0.6909** |
| p2_tip_f1 | 0.5200 | **0.6800** |
| p2_tax_f1 | 0.2857 | **0.4000** |
| charge_type_accuracy | 0.4000 | **0.6000** |
| p3_self_reconciled | 0.9103 | **0.9207** |
| p3_receipt_total_correct | 1.0000 | 0.9690 |
| rollup | 0.9200 | 0.9230 |

**The comparison is not paired** - the image sent to Mistral changed, so fresh
OCR noise is inside the delta. Per-case rollup stdev is 0.029, which makes the
+0.0030 rollup indistinguishable from noise. The p2 block carries the decision;
speed and visibly-straighter images carry the rest.

Inspected by eye, blobs was right on 5 of 5 disagreements. The one regression is
a crumpled Woolworths roll whose per-line angles fan from −6 to +14 deg: blobs
over-rotates to +5.19 where ~+2 is right. No cheap guard separates it - two
*correct* large corrections have wider angle spread (IQR 6.41, 6.11) than that
failure (4.78). Left alone.

### 2.26 Item discounts rewritten from OCR line order — LANDED

A receipt prints a promotion under the item it reduces. The model usually
attaches it (`item.adjustments`), but not on every call, and consensus selection
ranks on row counts and totals - so a candidate carrying per-item discounts
loses to one that lumped them into a single summary row. `PXL_20260801_194341311`
returned one `Nectar Price Saving -5.50` split proportionally across four items,
when the receipt prints −4.00 on the toilet roll, −1.00 on the muffins and −0.50
on the buns.

**Landed:** `receipt_scanning/discounts.py`. Parses promotions out of the OCR
markdown by position, votes across the run's calls, and `apply_to_ledger`
rewrites the winner's discount rows - one per item, adjacent to it, carrying
`parent_name`. Wired in as `_reattach_discounts` in `VerifiedConsensusStrategy`.

Two design points, both learned the hard way:

- It runs on the **finished ledger**, not on candidates. Attaching before
  post-processing changes which candidate selection prefers, and it started
  picking worse ones (a 3-row parse with grand 4.10 against a printed 13.35).
- It is kept only if the receipt still reconciles as well as before. That guard
  is what makes `PXL_20260604_201408239` correctly refuse: its voted set contains
  a misread −1.80 that would move the bill away from the printed total.

Nothing here reads the "YOUR SAVINGS TODAY" footer. Plenty of receipts do not
print one, so a trailing promotion is **discarded**, never used as a checksum.

| metric (147 cases) | before | after |
|---|---|---|
| p1_item_totals_f1 | 0.9280 | **0.9333** |
| p1_item_count_exact | 0.8857 | **0.8925** |
| p4_adjustments_f1 | 0.3529 | **0.4212** |
| rollup | 0.8920 | 0.8939 |

Two cases changed; everything already correct was left untouched, which is why
nothing regressed.

### 2.27 One minor unit of slack in the charge subset search — LANDED

`IMG-20260328-WA0000` prints items of 181.30, a 12.5% service charge of 22.65
and a total of 203.96 - a penny more than they sum to, because 12.5% of 181.30
is 22.6625. `select_additive_charges` found no reconciling subset and took its
"add nothing" branch, so **the entire 22.65 was dropped** and the party
underpaid by the whole service charge.

**Landed:** `MAX_UNITS_OFF = 1` and `_reconciles()`, replacing both
`abs(...) < tolerance` comparisons.

Counted in integer minor units, not compared against a float tolerance, because
a one-penny gap is not 0.01 in binary — `203.96 - 203.95` is
`0.010000000000019327`, so `<= tolerance` rejects it here and accepts it at
other magnitudes (`0.08 - 0.07` passes, `100.00 - 99.99` does not).

| metric (147 cases) | before | after |
|---|---|---|
| p2_charges_f1 | 0.7295 | **0.7536** |
| p2_tip_f1 | 0.7800 | **0.8133** |
| charge_type_accuracy | 0.6952 | **0.7429** |
| p3_self_reconciled | 0.8815 | **0.8884** |
| p3_grand_total_correct | 0.8735 | **0.8803** |
| p1_item_totals_f1 | 0.9280 | 0.9280 |

Exactly one case moved (0.374 -> 0.989 rollup). The concern recorded in that
branch's comment - that surfacing charges costs item-total F1 - does not apply:
that experiment surfaced charges which *never* reconciled, this one admits only
charges that reconcile to within the receipt's own rounding.

2.26 and 2.27 compose exactly: together rollup 0.8920 -> **0.8981**, the sum of
the two individual gains, no interaction.

### 2.28 Discount-to-item linkage — measured for the first time, partly fixed

`p4_adjustments_f1` compares a **multiset of amounts**. A discount attached to
the wrong item scores identically to one attached correctly, so linkage has
never been measured. A new script pairs (parent item, amount) against the labels,
which do carry the right parent.

| linkage over the 11 cases with item-level discounts (220 discount-runs) | F1 | precision | recall |
|---|---|---|---|
| before | 0.4785 | 0.5855 | 0.4045 |
| after | **0.5161** | 0.6316 | 0.4364 |

Three changes: `expand_discount_rows` emits `parent_name`;
`link_orphan_discounts` names a parent for discounts that arrived via the
receipt-level charge path; and a real bug - Mistral renders some receipts as
markdown tables, and the header `| Qty | Item | Price | Total |` tripped the
totals-block detector on the *first row*, silencing discount parsing for every
table-rendered receipt. That fix alone took `f4059c83` from 0/5 to 5/5.

**The ceiling is not linkage.** Of 124 remaining misses, **70 are one case** -
`17392`, where the model produces zero negative rows for 14 labelled discounts.
Add `17400` (reads −0.15 as −0.75 in all 15 calls) and
`PXL_20260801_193448336` (emits one `TOTAL DISCOUNT -0.60` for two −0.30s), and
almost the whole recall gap is discounts never extracted or pre-aggregated.

`parent_name` is a name, not an identity: `4bf96933` has two identical
`PRYMAT 3 FOR £1.20` discounts under different items and is still 0/10. A
`parent_index` would be unambiguous and no harder to emit.

### 2.29 Receipt-level discounts — the worst group in the corpus, unsolved

Cases grouped by what kind of discount the label carries:

| group | n | rollup | item F1 | self-reconciled |
|---|---|---|---|---|
| receipt-level discount only | 4 | **0.4993** | 0.9292 | **0.2500** |
| item-level + summary | 10 | 0.7698 | 0.7988 | 0.7400 |
| item-level only | 1 | 0.9972 | 1.0000 | 1.0000 |
| no discounts | 132 | 0.9227 | 0.9432 | 0.9262 |

**Across all 4 cases and all 20 runs, zero discount rows and zero adjustments
are produced.** Item extraction is fine; everything lost is in the charges.

The cause is that `select_additive_charges` is all-or-nothing: when no subset
closes the gap, *every* charge is discarded. `cebb2ce9` loses a Service Charge
of 19.70 and GST of 17.34 because a −197.00 discount line cannot be reconciled
alongside them. `IMG-20260521-WA0027` loses both its charges because the OCR read
the printed total 20p low.

Read the 0.4993 carefully - of the four, `a5e2d31b` is **already correct** (the
15% desconto is baked into the item prices; items = receipt total = 131.00), and
`cebb2ce9`'s label does not close arithmetically (185.00 + 37.04 != 234.04). Two
real failures out of four is a thin and partly unreliable basis.

Also unmeasured: `expected_adjustments` is built only from item-level label
adjustments and `expected_charges` covers tax and tip roles, so **no metric
scores a whole-bill discount amount at all**.

The fix direction is not more tolerance - it is that failing to place one charge
should not delete the others. With 3 usable cases the corpus cannot settle it;
more labelled receipt-level discounts are the prerequisite.

### 2.30 The 2.29 fix — LANDED. Whole-bill discounts as adjustments — REJECTED

2.29's diagnosis was right and the fix is four lines. `select_additive_charges`
now keeps the plausible subset that gets **closest** to `receipt_total` when no
subset closes it exactly, instead of discarding every charge. Moving *away* from
the printed total is never allowed, which is what separates this from the
rejected "surface them anyway" (that one cost 0.010 of item-total F1).

| 147 cases x 5 runs | baseline | **best-effort subset** |
|---|---|---|
| p1_item_totals_f1 | 0.9334 | **0.9343** |
| p2_charges_f1 | 0.7536 | **0.8286** |
| p2_tax_f1 | 0.6585 | **0.6824** |
| p2_tip_f1 | 0.8133 | **0.9267** |
| charge_type_accuracy | 0.7429 | **0.8571** |
| p3_self_reconciled | 0.8952 | **0.8966** |
| p3_grand_total_correct | 0.8884 | **0.8966** |
| p4_adjustments_f1 | 0.4115 | 0.4023 |
| s_item_name | 0.8876 | 0.8859 |
| rollup | 0.9013 | **0.9069** |
| identical across runs | 115/147 | **116/147** |

Fires 224 times across the run. `cebb2ce9` is the case 2.29 named: it recovers a
19.70 service charge and a 17.34 GST that an unplaceable -197.00 discount used
to delete, bill 145.00 -> 182.04. p1 goes **up** and stability goes **up**, so
the standing "reject anything that lowers p1" rule is not in play. The two small
losses are p4 (the metric 2.5 established cannot measure discounts) and
s_item_name at -0.0017.

**Rejected in the same pass: giving a whole-bill discount its own adjustment**
rather than a negative row at the bottom of `items` — `_ADJUSTMENT_TYPES` gaining
`"discount"` on the charge side only.

| | baseline | + discount adjustments |
|---|---|---|
| p1_item_totals_f1 | **0.9334** | 0.9287 |
| p1_item_count_exact | 0.8952 | **0.9143** |
| p4_adjustments_f1 | **0.4115** | 0.3722 |
| rollup | **0.9013** | 0.8997 |
| identical across runs | **115/147** | 114/147 |

Lowers p1, p4, the rollup and stability together to buy item-count exactness.
Retry only once a metric scores a receipt-level discount **amount** — as 2.29
records, none does, so this change was flying blind in the direction the numbers
say is wrong.

**Also landed, unmeasurable by design: `parent_uid`.** Discount rows now carry a
stable id for the row they reduce, alongside the `parent_name` the v1 client
reads. 2.28 left this open — `4bf96933` prints two identical
`PRYMAT 3 FOR £1.20` savings under different items and scored 0/10 on linkage
because a name is not an identity. No metric moved (no metric scores linkage),
and none was expected to; the check is the assertion in `discounts.demo()`.

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

**Latest: 147 cases x 5 runs = 735 scans**, `strategy:verified_consensus`,
captures in `labeler/ocr_captures/` (blob deskew).

| metric | value |
|---|---|
| p1_item_totals_f1 | 0.9334 |
| p1_item_count_exact | 0.8952 |
| p2_charges_f1 | 0.7536 |
| p2_tax_f1 | 0.6585 |
| p2_tip_f1 | 0.8133 |
| charge_type_accuracy | 0.7429 |
| p3_self_reconciled | 0.8952 |
| p3_grand_total_correct | 0.8884 |
| p3_receipt_total_correct | 0.9628 |
| p4_adjustments_f1 | 0.4115 |
| rollup | **0.9013** |
| identical across 5 runs | 115/147 |

Session progression on the same 147 cases: 0.8920 -> 0.8981 (2.26 + 2.27) ->
**0.9013** after re-capturing three cases whose stored images were rotated
+16.10 deg by the pre-`SATURATION_MARGIN` search. That last step is *unpaired* -
fresh OCR on three receipts - so read the +0.0032 as directional.

**Corpus staleness is now a checkable property.** Every capture stores the
`deskew_angle` it was made with; comparing that against what the current code
produces found 26 of 147 stale, 3 of them by 16 deg. The >1 deg heuristic used
to pick re-capture candidates missed those, because they were stale for an
*older* reason. Re-run that check after any deskew change - it costs nothing:

```python
rec = json.loads(next((OCR_CAPTURES/name).glob("*.json")).read_text())
abs(rec["deskew_angle"] - detect_angle(cv2.imread(str(image_path)))) > 0.05
```

23 sub-degree cases remain stale and were deliberately not re-captured: 345
calls to rotate receipts by under a degree buys noise, not accuracy.

---

### Earlier: 72 cases x 5 runs = 360 scans, same OCR for both columns (post-processing only).

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

### Alongside 2.25-2.29

- **Capture directories renamed** so the defaults point at current data:
  `labeler/ocr_captures/` is now the blob-deskew set (147 cases), and the old
  projection-deskew captures are archived at
  `labeler/ocr_captures_projection_deskew/` (126 cases). The two are **not
  comparable** - the image sent to Mistral differs. Before the rename,
  `capture_labelled_ocr` would have quoted 315 calls against the stale
  directory and re-captured cases that already existed.
- `receipt_scanning/discounts.py` is new, with an assert-based `demo()`
  covering both receipt dialects and the idempotence of a second pass. Run it
  directly: `python ninjatab/tabs/receipt_scanning/discounts.py`.
- `deskew.py` gained `text_rects`, `detect_text_rects`, `_character_mask`,
  `_paper_box` and its own `demo()` over synthetic skewed receipts.
- The `discounts:` replay pipelines were removed once the behaviour shipped, so
  `strategy:verified_consensus` measures the shipped code rather than a variant.
- 117 tests pass.

## 7. Production was running the unmeasured strategy (2026-08-09)

### 7.1 The finding

A production scan log for tab `019f78bb-bac4-7002-bce6-ee6a04b6c220` reads
`via concurrent_consensus`. Everything in sections 2-6 above was measured on
`verified_consensus` — `evaluate_captures.DEFAULT_PIPELINE` is
`strategy:verified_consensus`, and the no-arg fallback is the same. It could not
have been otherwise: `rollup`, `self_reconciled`, `adjustments` and
`charge_type_accuracy` only exist on the ledger path, and
`ConcurrentConsensusStrategy.post_process` never calls `reconcile_ledger`.

The registry default is `baseline_mistral_ocr` (`utilities/registry.py`), so the
live `scan_strategy` Option was hand-set to `concurrent_consensus` at some point
and never moved. **Every number in this document describes a pipeline production
was not running.**

### 7.2 Three-way measurement

198 labelled cases, 5 runs x 3 calls = 990 observations, zero failures. Paired
over identical cached OCR (`labeler/ocr_captures/`, blob deskew), so the two
differ only in post-processing. No API calls.

```
python manage.py evaluate_captures --compare strategy:concurrent_consensus \
    strategy:verified_consensus v1:verified_consensus
```

| metric | w | concurrent (prod) | verified | delta |
|---|---|---|---|---|
| **rollup** | | **0.8600** | **0.9180** | **+0.0580** |
| p3_self_reconciled | 5.5 | 0.8184 | 0.9092 | +0.0908 |
| p1_item_totals_f1 | 5.0 | 0.9247 | 0.9415 | +0.0168 |
| p2_charges_f1 | 3.0 | 0.6624 | 0.8085 | +0.1461 |
| p3_grand_total_correct | 3.0 | 0.8172 | 0.9091 | +0.0919 |
| p3_receipt_total_correct | 1.0 | 0.9724 | 0.9724 | 0.0000 |
| p4_adjustments_f1 | 0.75 | 0.3329 | 0.4073 | +0.0744 |
| p1_item_count_exact | | 0.8899 | 0.9051 | +0.0152 |
| p2_tax_f1 | | 0.6111 | 0.6889 | +0.0778 |
| **p2_tip_f1** | | **0.1875** | **0.9062** | **+0.7188** |
| **charge_type_accuracy** | | **0.2244** | **0.8026** | **+0.5782** |
| s_item_name | 0.5 | 0.8921 | 0.8913 | -0.0008 |
| s_establishment | 0.3 | 0.9138 | 0.9138 | 0.0000 |
| s_translated_name | 0.2 | 0.8451 | 0.8464 | +0.0013 |
| s_date_match | 0.2 | 0.9407 | 0.9407 | 0.0000 |
| s_currency_match | | 0.9606 | 0.9606 | 0.0000 |
| identical across 5 runs | | 143/198 | 157/198 | +14 |
| mean rollup stdev | | 0.0362 | 0.0240 | -34% |

Every weighted metric improves or is flat. The one regression is `s_item_name`
at -0.0008, which is noise.

### 7.3 What production has been losing

**Tips, almost entirely.** `p2_tip_f1` 0.1875. Without the ledger,
`standard_post_process` forces charges into the item list to make `sum(items)`
come out right, instead of surfacing them as typed charges — so a tip mostly
lands as an unlabelled item row. `charge_type_accuracy` 0.2244 is the same cause.

**Every discount mechanism.** `expand_discount_rows`,
`drop_restated_discount_summary` and `link_orphan_discounts` are all reached only
via `reconcile_ledger` / `VerifiedConsensusStrategy`. The savings-footer repair
landed alongside 2.25-2.29 has therefore **never executed on a real user scan**.

Item extraction improves too (+0.0168 p1) even though this is a post-processing
change: `verify_and_repair` runs per candidate *before* selection, so
`select_best_line_items` chooses among parses that already reconcile.

It is also more deterministic — 14 more cases identical across all 5 runs, and
run-to-run rollup variance down a third. No accuracy/stability trade.

### 7.4 The v1 presenter still costs nothing — kept

| | verified (v2) | v1:verified | delta |
|---|---|---|---|
| rollup | 0.9180 | 0.9176 | -0.0004 |
| p4_adjustments_f1 | 0.4073 | 0.4073 | **0.0000** |
| charge_type_accuracy | 0.8026 | 0.7833 | -0.0193 |
| s_item_name | 0.8913 | 0.8896 | -0.0017 |

Adjustment linkage survives flattening **exactly**. The only real cost is
`charge_type_accuracy`, which is precisely `flatten_for_v1` rewriting
`category: "discount"` -> `"service"` ("the old client has never heard of
discount"). The app recovers the type for free from sign + absence of
`parent_uid`, so **the mobile client should stay on v1**. Moving it to v2 would
buy a discriminator it can already compute, in exchange for a second annotation
shape to parse and multi-tax rows its single tax editor cannot represent.

### 7.5 A live case the ledger would have caught

Spice Store, `019f78bb-bac4-7002-bce6-ee6a04b6c220`:

```
items: [{name: 'White & Grey Sneaker', total: '2550.00', quantity: 1,
         price_per_quantity: '2550.00',
         adjustments: [{name: 'Discounts (10%)', amount: '-255.00',
                        is_new_price: False}],
         receipt_line_text: 'White & Grey Sneaker 1 2550.00 2550.00'}]
receipt_total: '2295.00'   items_total: 2550.0   totals_reconciled: False
```

No top-level `adjustments` key, so `flatten_for_v1` returned early and did
nothing. The discount stayed nested where the client never looks, and the user
saw one 2550 item against a 2295 receipt.

It also breaks `expand_discount_rows` **in the opposite direction from 2.14**.
There the model grossed items up and the pipeline had to reverse it; here `total`
is already the pre-discount price (2550 - 255 = 2295 = `receipt_total`), so the
gross-up would emit 2805 + (-255) = 2550 and still miss by 255. The function's
"the model reports `total` as the price actually paid" assumption is a coin flip,
not a rule.

### 7.6 Where receipt-level discounts actually arrive

Confirmed by running `reconcile_ledger` + `flatten_for_v1` over the ground truth
for `IMG-20260521-WA0027.jpg`:

```
charge_selection: best_effort_closest   reconciled: False   gap: -0.20
v2 adjustments:  Service Charge  3.98  type=tip  split=proportional
v1 items:  Churrasco 24.50 (item) | FULL Costillar 21.00 (item)
           Discount (The fork) -13.85 (service, parent_uid=None)
           Service Charge 3.98 (tip)
```

Two model channels — `other_charges` with a negative amount (what the labels
use) and `items[].kind: "discount"` — converge on the same wire shape: a negative
row that **stays in `items`**, because `_ADJUSTMENT_TYPES` is `("tax", "tip")`
only. Discounts never become v2 adjustments, item-level or receipt-level.

So the client discriminator needs no backend change and no v2:

| shape | meaning | client treatment |
|---|---|---|
| negative row, `parent_uid` set | item discount | indented red row under its parent, nets into it |
| negative row, no `parent_uid` | receipt-level discount | its own charge line beside TAX/TIP, proportional |

The `tax: 5.31` on that case is dropped as non-additive (read as VAT-inclusive),
and the -0.20 gap is the `receipt_total` OCR misread already noted at
`select_additive_charges`.

### 7.7 Path forward for discounts

**Ordered. Step 1 is a prerequisite, not an option — nothing below it runs
without the ledger.**

1. **Flip `scan_strategy` to `verified_consensus`.** One Option value, no extra
   Mistral calls, +0.0580 rollup, and it is the only way any discount code
   reaches production. Measured in 7.2.

2. **Label 10-15 more discount receipts.** `p4_adjustments_f1` at 0.4073 is the
   worst metric on the board even after the switch, off a corpus of 7
   `has-item-discount` and 2 `has-receipt-discount` cases — one of which
   (`17393.jpg`) is still unannotated. Section 2.5 found the labelling
   convention nets discounts into item totals; that must change too, or the
   metric keeps measuring the convention rather than the extraction. **Do this
   before step 3** — otherwise the fix is being judged on one Spice Store scan.

3. **Make `expand_discount_rows` decide gross-vs-paid arithmetically.** Try
   `total` as the paid price and as the pre-discount price; keep whichever lands
   the bill on `receipt_total`; fall back to today's assumption when neither
   closes or there is no total. Same philosophy as `select_additive_charges` —
   let the arithmetic decide rather than encoding a rule about how receipts
   print. This is the 2.14 / 7.5 defect.

4. **App: a `DISCOUNT` charge row.** A parentless negative row becomes a third
   `ReceiptChargeRow` beside TAX and TIP — right place on the paper, already
   `proportional` in `_SPLIT_BY_TYPE`, and the widget exists. Render it **only
   when one was scanned**; most receipts have none and it should not cost a
   permanent line. Manual entry of a bill-level discount is YAGNI — a negative
   line item covers it.

**Deliberate no-ops, both load-bearing:**

- **No client-side savings-footer heuristic.** `drop_restated_discount_summary`
  identifies the footer arithmetically *and* reverses the per-item gross-ups in
  the same move; dropping the footer alone broke closure on all 17 scans where it
  was detected. The client cannot do that half. Two systems disagreeing about
  which negative row is real is worse than either alone. Residual gap, accepted:
  the >=2-discounts guard means one item discount plus a "YOUR SAVINGS" footer is
  undetectable, because a single real discount reported twice is arithmetically
  identical to it.

- **Do not relax `_plausible_charges` for tips.** It caps additive charges at the
  item subtotal, which loses a generous handwritten tip
  (`diner-pays-tip-...jpg`: tip 100.00 on a 32.43 bill, `charge_selection:
  unreconciled_none_added`, tip dropped). Keep it anyway: the costs are
  asymmetric — a dropped tip is a two-second correction in the app's TIP field, a
  phantom tip invents money the payer is asked to split — and the guard is what
  stops a degenerate parse that files the whole bill as one fee from reconciling
  against an empty item list and *winning* consensus selection. A modest
  handwritten tip that closes the gap is already handled (`subset_reconciled`).

  Note that case's ground truth is internally inconsistent (`tip: 100.00`,
  `receipt_total: 10032.43`, items 32.43 — the viral photo is a $10,000 tip). It
  can never reconcile as labelled and is a poor baseline until re-checked.

### 7.8 Orphan linking driven by amount, not wording — kept

`PROMO` (`discounts.py:30`) gated `parse_line_discounts` on English loyalty
wording — `nectar`, `clubcard`, `special offer`, `saving`. Two live scans showed
what that costs: LIDL's `Price Cut` and a US store's `less promo` match none of
it, so their discounts reached the client unparented. Adding those two phrases
would have moved the cliff, not removed it.

**`link_orphan_discounts` never needed to discover anything.** By the time it
runs, the reduction is already a row in the annotation — the amount is known.
So `locate_discounts(markdowns, amounts)` inverts the search: nominate the line
carrying a *known* figure and take the preceding priced line as its parent. An
amount is an amount in every language and script; `_similar` already binds
text to row without reading words.

The Sainsbury's problem `PROMO` existed for — "Nectar Price Saving £0.60"
printed unsigned for -0.60 — disappears rather than needing solving: searching
for a known 0.60 finds the line whether or not the OCR kept the minus. An
explicitly negative token is still preferred over an unsigned one, so an item
priced the same as a saving elsewhere cannot capture it.

`MONEY` was a second, unnoticed locale trap: it requires a `£$€` sign, so a
receipt printing bare figures (LIDL) or any other symbol was invisible to the
whole module. The locator scans with `NUMBER`, sign optional. **`MONEY`,
`PROMO`, `parse_line_discounts` and `vote_discounts` are untouched** — the
lumped-summary rewrite in `apply_to_ledger` genuinely does need discovery (it
splits one -5.00 into per-item amounts it does not know), so it keeps the
behaviour it was measured with.

An orphan may only take a saving its item does not already carry. A first
measurement of the locator alone looked like a clean win on linkage and was
**wrong**: counting attachments says nothing about whether they are right, and
the bare change was quietly reducing two items twice by the same money. Tesco
prints the cause —

    2 Heinz Beanz In Tomato Sauce 415g   £3.20
    Cc Any 2 For £2.20                  -£1.00      <- the saving
    Subtotal:                           £29.65      <- `\btotal\b` does not match "Subtotal"
    Savings:                            -£1.00      <- the same £1.00 again

— and the annotation carries that £1.00 twice too: once expanded from the
nested adjustment and correctly attached, once as a bare row. So a saving of a
given size may attach to a given item once. The *parent* is what separates this
from two genuine identical multibuys (4bf96933 prints two "PRYMAT 3 FOR £1.20"
savings under *different* items, and both are real). Widening `TOTALS` to know
the word "Subtotal" would have been the wording trap again.

Three-arm paired replay, 990 scans, same cached OCR:

| | linked to a parent | same saving twice on one item |
|---|---|---|
| `PROMO` wording gate | 124/207 (59.9%) | 1 row / 1 scan |
| amount-driven, no guard | 153/207 (73.9%) | **3 rows / 2 scans** |
| amount-driven + guard | **151/207 (72.9%)** | 1 row / 1 scan |

27 more attachments, and the double-charge count returns to the pre-existing
baseline. That remaining one predates this work and appears in every arm.

Every metric scored at the time was bit-identical — rollup 0.9180,
`p4_adjustments_f1` 0.4073, stability 157/198 — because `p4_adjustments_f1`
compares discount amounts as a *multiset* (`scorer.py:350`) and cannot tell a
saving on the right item from the same saving on the wrong one. The negative-row
count is unchanged at 207 throughout, so nothing was invented or dropped.

Counting attachments was the wrong measurement and it took a wrong turn to see
it: row names are no guide either, because the same orphan is named
"Cc Any 2 For £2.20" in one run of a scan and "Savings" in another, which made
an eyeball pass over the new binds actively misleading. **7.9 scores this
properly against the labels.**

Not all 56 remaining orphans are failures: a genuine bill-level discount has no
parent and must not acquire one.

`discounts.py`'s `demo()` gained the wording-free cases: LIDL `Price Cut` bound
to the right two items, German `Preisvorteil` and Turkish `indirim`, a saving
past the totals block staying unbound, and a signed saving winning over an
unsigned figure of the same size. Run it directly:
`python ninjatab/tabs/receipt_scanning/discounts.py`. 127 tests pass.

### 7.9 Scoring attachment, not just amounts — `p4_discount_links_f1`

2.5 concluded the corpus could not measure discount extraction. That is no
longer true: the labeller UI attaches a discount to its item, and the labels
record the item **gross** with the saving nested in `adjustments` — 8 cases,
~35 discounts, including Czech LIDL (`SLEVA 25%`, `Lidl Plus sleva`) and the
Tesco restatement whose truth is one -1.00 on Heinz Beanz with `Savings -1.00`
and `Promotions -1.00` sitting in `other_charges` as footers.

`p4_adjustments_f1` cannot see any of that: it compares amounts as a multiset,
so all three arms of 7.8 score identically on it. The new
`p4_discount_links_f1` pairs each amount with the item it is attached to —
`_label_links` / `_result_links` / `_links_f1` in `labeler/evaluation/scorer.py`
— matching item names by token overlap so OCR wording drift does not break it.
It reads a linked row (`parent_uid`) and a still-nested adjustment alike, so v1
and v2 shapes score the same. `None` when a label records no discounts, so an
ordinary receipt neither rewards nor punishes.

**Weight 0 in `PRIORITY_WEIGHTS`**, following the `charge_type_accuracy`
convention: every figure recorded above was measured without it, and `rollup`
must stay comparable. Verified — rollup is still 0.8600 / 0.9180.

It is `None` where the label records no links, following the
not-scored-when-absent convention used for tax and tip — and not only because a
discount-free receipt should not be rewarded. Five corpus cases *have*
discounts the labeller did not attach (2.5 names `f52eb086` and `f71c4328`;
`PXL_20260806` carries a "GORALKI- 3 FOR 1.30" saving with no labelled
adjustment; also `6984a385` and `IMG-20260516-WA0002`). Scored strictly they
contributed **25 of 44 false positives** — measuring the labelling, not the
pipeline. The cost is a blind spot: a discount invented on a receipt that has
none goes unpunished. Closing it means labelling those five, not changing the
metric.

Through `evaluate_captures`, per-case average:

| | `p4_adjustments_f1` | `p4_discount_links_f1` |
|---|---|---|
| `concurrent_consensus` | 0.3329 | **0.2255** |
| `verified_consensus` | 0.4073 | **0.6024** |

So production was not merely mislaying discounts, it was attaching almost none
of them — another cost of 7.1 that no existing metric reported.

Pooled over every labelled discount (micro-averaged, so it weighs a 14-discount
Sainsbury's receipt above a 1-discount one), the three arms of 7.8:

| arm | TP | FP | FN | precision | recall | F1 |
|---|---|---|---|---|---|---|
| `PROMO` wording gate | 90 | 19 | 130 | 0.826 | 0.409 | 0.547 |
| amount-driven, no guard | 107 | 21 | 113 | 0.836 | 0.486 | 0.615 |
| amount-driven + guard | 107 | **19** | 113 | **0.849** | **0.486** | **0.618** |

**+17 correct attachments; F1 0.547 -> 0.618, and precision rises too**
(0.826 -> 0.849). A first pass that scored the under-labelled cases as errors
showed precision *falling* 0.726 -> 0.709; those 12 "extra wrong binds" were
mostly correct links against truth the labels do not record. The guard removes
2 false positives and loses no true ones — exactly the 2 double-charged savings
7.8 counted structurally.

Recall at 0.486 is the honest headline: **more than half of the discounts the
labeller attached by hand are still not attached by the pipeline.** That is the
number to move next, and it is now visible in the standard report rather than
in a one-off script.

### 7.10 What five production scans show

All `verified_consensus`, captured before 7.8. They are worth adding to the
corpus: between them they cover three discount dialects the 8 labelled cases do
not.

**Tymbark (Polish supermarket, GBP)** — `GORALKI- 3 FOR 1.30`, -0.47,
`kind: "discount"`, **no `parent_uid`**. `PROMO` matches nothing in that string,
which is the 7.8 failure exactly; the amount-driven locator binds it to
`GORALKI`. The same receipt is in the corpus as `PXL_20260806_131708939.jpg`
with **no labelled adjustment**, which is why that correct link scored as a
false positive until 7.9 stopped scoring unlabelled cases. Two other defects on
that scan, both unrelated to discounts: only two `GORALKI` rows were extracted
where the 3-for-1.30 implies three, and `totals_reconciled` is False on a 1p gap
(grand_total 25.40 vs receipt_total 25.39).

**Adult Tee (US apparel)** — a *third* gross-up dialect. The printed figure is
the price **after** the discount (`Adult Tee x 1 $8.09`, pipeline emits 8.99
with a -0.90 beneath it), where Sainsbury's prints the price **before** it. So
`drop_restated_discount_summary` finds its summary (`Savings -2.00` equals the
two discounts) and then correctly **refuses**: un-grossing to 8.09 while keeping
-0.90 would give 15.98 against a receipt_total of 19.73. The closure guard
earns its place here. But the gross-up survives, so the user is shown 8.99 for
an item the receipt prices at 8.09 — 2.14 and 7.5 again, from the other
direction.

**Tesco meal deal, twice, same receipt** — one run emits the three `Meal Deal`
rows nested, so `expand_discount_rows` assigns uids and all three link; the
other emits them flat and **none** link, even though `meal deal` *is* in
`PROMO`. Identical input, different linkage. The stability figure in every table
above tracks `rollup`, which is unmoved by this, so linkage instability has been
invisible; `p4_discount_links_f1` is what would expose it.

**Duplicate footers are the norm, not the exception.** `Savings -2.90` *and*
`Promotions -2.90`; `Savings -1.00` *and* `Promotions -1.00`; `Discount -4.00`
*and* `BSK DISCOUNT -10.00`. `drop_restated_discount_summary` matches a single
charge equal to the sum of the discount rows, so the split-summary shape (7.5)
and the doubled-footer shape both slip past it.

### 7.11 Corpus 198 -> 212, and the linkage change holds up

14 new labelled cases captured (`capture_labelled_ocr`: 210 API calls, 2 lost to
a 503 on the Morrisons `.webp` and picked up by a re-run, which only calls for
what is missing). 1060 observations.

**Figures are not comparable across corpus sizes** — the same caveat as the
66 -> 111 growth in section 2. Everything above was measured at 198.

The new cases are harder, deliberately so:

| | new 14 | all 212 |
|---|---|---|
| p1_item_totals_f1 | 0.8864 | 0.9378 |
| p1_item_count_exact | **0.6000** | 0.8849 |
| p2_charges_f1 | 0.5000 | 0.7959 |
| p3_self_reconciled | 0.8429 | 0.9048 |
| p4_adjustments_f1 | 0.5278 | 0.4428 |
| p4_discount_links_f1 | 0.5780 | 0.5948 |
| rollup | 0.8581 | 0.9140 |
| identical across runs | 10/14 | 167/212 |

Item *counting* is where they hurt (0.60): these are long, crumpled,
screenshot and app-render receipts. Discount handling on them is no worse than
the corpus average, which is the first evidence that the discount work
generalises past the Tesco/Sainsbury's dialects it was built on.

**They close the 7.7 step 2 gap, and for the right kind of discount.** 13 of 14
carry a discount, and **12 carry a bill-level one** where the corpus previously
had a single usable example (`IMG-20260521-WA0027`). Item-level discounts are up
too — 5 more cases, one of them 5 discounts on 4 items.

Whole corpus at 212 cases:

| metric | concurrent | verified | delta |
|---|---|---|---|
| rollup | 0.8368 | **0.9140** | +0.0772 |
| p3_self_reconciled | 0.7790 | 0.9048 | +0.1257 |
| p1_item_totals_f1 | 0.9190 | 0.9378 | +0.0188 |
| p2_charges_f1 | 0.6356 | 0.7959 | +0.1603 |
| p2_tip_f1 | 0.1875 | 0.9062 | +0.7188 |
| charge_type_accuracy | 0.1620 | 0.7796 | +0.6176 |
| p4_discount_links_f1 | 0.1550 | 0.5948 | +0.4398 |
| identical across runs | 154/212 | 167/212 | +13 |

The strategy gap **widened** with the harder cases, 0.0580 -> 0.0772: the
receipts that defeat `concurrent_consensus` are exactly the ones the ledger
repairs. `v1:verified_consensus` still costs -0.0003 of rollup.

Three-arm linkage (7.8, 7.9) re-measured at 212, scored only where the labeller
attached links — now 280 of them, up from 220:

| arm | TP | FP | FN | precision | recall | F1 |
|---|---|---|---|---|---|---|
| `PROMO` wording gate | 102 | 29 | 178 | 0.779 | 0.364 | 0.496 |
| amount-driven, no guard | 135 | 36 | 145 | 0.789 | 0.482 | 0.599 |
| amount-driven + guard | 135 | **34** | 145 | **0.799** | **0.482** | **0.601** |

**+33 correct attachments, F1 0.496 -> 0.601** — a larger gain than the +0.071
measured at 198, with precision rising again (0.779 -> 0.799). The guard removes
the same 2 false positives and loses no true ones. The wording gate degrades on
the new cases (recall 0.409 -> 0.364) exactly as predicted: more receipts, more
dialects it has never heard of.

Recall 0.482 remains the number to move.

### 7.12 How discounts are labelled — the convention `p4_discount_links_f1` reads

Recorded explicitly because 2.5 drew a conclusion from an older convention and
this document carried it for months: "the labelling convention nets discounts
into item totals, so the dataset structurally cannot measure discount
extraction". That has not been true since the labeller UI gained per-item
attachment. Anyone reasoning about discount metrics should read this section and
not 2.5.

**An item-level discount is nested on the item it reduces, and the item is
recorded GROSS.**

```json
{"name": "2 Heinz Beanz In Tomato Sauce 415g", "total": "3.20",
 "adjustments": [{"name": "£1.60 each Cc Any 2 For £2.20",
                  "translated_name": "...", "amount": "-1.00",
                  "is_new_price": false}]}
```

The receipt charged £2.20 for that row. The label stores the **pre-discount**
3.20 with the -1.00 delta beside it; `labels.net_item_total` is what nets them.
So a label's item total is not what the shopper paid for that line, and any
comparison against `item["total"]` alone will be wrong by the discount.

`amount` is a **negative delta**. `is_new_price: true` inverts that — the line
replaced the price outright ("was 5.00, now 3.00"), so `amount` is the new price
rather than a reduction. **No label in the corpus uses it** (0 of 56), so that
branch is untested against real data.

**A bill-level discount is a negative `other_charges` entry**, with no link to
any item — that absence *is* the signal that it applies to the whole receipt:

```json
"other_charges": [{"name": "Celková sleva", "translated_name": "Total discount",
                   "amount": "-5.87"}]
```

**Footer restatements land there too, and are frequently doubled.**
`IMG-20260328-WA0027` records `Savings -5.87` *and* `Promotions -5.87` — one sum
of money, printed twice, labelled twice, and both are legitimate transcriptions
of what the receipt shows. A reader of these labels cannot assume a negative
`other_charge` is money off the bill; it may restate savings already nested on
the items. 7.10 shows the same doubling in production.

Coverage at 212 labelled cases:

| | cases | rows |
|---|---|---|
| item-level (nested `adjustments`) | 16 | 56 |
| bill-level (negative `other_charges`) | 26 | 32 |
| `is_new_price` | 0 | 0 |

**Known gaps.** Five cases carry discounts that were never attached in the UI
(`f52eb086`, `f71c4328`, `PXL_20260806` Tymbark, `6984a385`,
`IMG-20260516-WA0002`). `p4_discount_links_f1` returns `None` where a label
records no links, precisely so those score as "unknown" rather than as pipeline
errors — scoring them strictly moved precision 0.849 -> 0.709 and was measuring
the labelling. The cost is that a discount invented on a genuinely
discount-free receipt is not punished. Labelling those five closes it; changing
the metric does not.

### 7.13 Two ways to raise linkage — both REJECTED, both measured

Recall sat at 0.482 with 280 labelled links. Decomposing the misses first, which
is what made both of these answerable:

| | rows | share |
|---|---|---|
| attached correctly | 135 | 48.2% |
| missed, **the amount is in the output** | 61 | 21.8% |
| missed, amount absent entirely | 84 | 30.0% |

So any attachment fix has a ceiling of +21.8pp (recall ~0.70). The other 30% is
money the model never emitted — no routing, prompt or schema change reaches it,
and with `p1_item_count_exact` at 0.60 on the new cases whole rows are being
dropped. Caveat: that bucket conflates "not read" with "folded into the item
price", which the Adult Tee shape in 7.10 does; nothing here separates them.

**Selection tiebreak — REJECTED.** `apply_to_ledger` has always noted that "a
candidate carrying per-item discounts loses to one that lumped them into a
summary", and `select_best_line_items` ranks on reconciliation, transcribed
fraction and modal item count, none of which sees attachment. Added
`attached_fraction` as rank 4 (share of a candidate's savings naming their item,
a fraction so emitting more savings is never rewarded, 1.0 when there are none).

Candidates disagree on attachment in **69 of 1060 scans (6.5%)**, but the higher
ranks already decide all but **4 (0.4%)** — and those 4 net-lost: linkage TP
135 -> 134, `p4_discount_links_f1` 0.5948 -> 0.5906, `p4_adjustments_f1` 0.4428
-> 0.4369, `p1_item_totals_f1` 0.9378 -> 0.9374, rollup 0.9140 -> 0.9138.

The reason is structural and worth keeping: `link_orphan_discounts` runs on the
**winner, after selection**, so since 7.8 the amount-driven locator already
recovers the flat candidate. Preferring the nested one buys almost nothing and
costs whenever it is marginally worse at item totals. The docstring's complaint
was true when it was written and has been fixed elsewhere.

**Positional default — REJECTED.** "Attach every unparented saving to the item
above unless we know it is receipt-level" (knowing = it came from
`other_charges`), inverting the current evidence-to-attach rule:

| | TP | FP | FN | precision | recall | F1 |
|---|---|---|---|---|---|---|
| evidence to attach (current) | 135 | **34** | 145 | **0.799** | 0.482 | **0.601** |
| positional default | 141 | 56 | 139 | 0.716 | 0.504 | 0.591 |

**6 more right for 22 more wrong.** The F1 loss understates it, because the two
errors do not cost the same: a wrong attachment nets a saving onto an item one
person claims, so that person pockets it and everyone else pays more, while an
unattached saving stays proportional and is mildly wrong for everyone. Silence
is the cheaper failure, so attachment should keep requiring evidence.
