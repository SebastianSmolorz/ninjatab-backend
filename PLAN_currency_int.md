# Implementation Plan: Currency Storage Conversion — Decimal to Integer (Minor Units)

## Overview

Convert all currency-value storage fields from `DecimalField` to `IntegerField` (storing minor currency units, e.g. cents). The key principle: **the database stores integers; the API boundary converts to/from human-readable Decimal; internal arithmetic operates in integer minor units, with Decimal only for exchange-rate multiplication**.

---

## Step 0: Foundational Utility — `currency_utils.py`

**File:** `backend/ninjatab/tabs/currency_utils.py` (new file)

```python
CURRENCY_DECIMAL_PLACES: dict[str, int] = {
    'JPY': 0,
    'HUF': 0,
    # all others default to 2
}

def get_decimal_places(currency_code: str) -> int:
    return CURRENCY_DECIMAL_PLACES.get(currency_code, 2)

def minor_to_decimal(amount_minor: int | None, currency_code: str) -> Decimal | None:
    """Convert integer minor units to display Decimal. E.g. 1050, 'USD' → Decimal('10.50')"""

def decimal_to_minor(amount: Decimal, currency_code: str) -> int:
    """Convert display Decimal to integer minor units. E.g. Decimal('10.50'), 'USD' → 1050
    Uses int(round(amount * 10**dp)) to avoid floating-point drift."""

def minor_to_minor(amount_minor: int, from_currency: str, to_currency: str, rate: Decimal) -> int:
    """Convert minor units between currencies using a rate, returning int minor units.
    Handles differing decimal places (e.g. USD→JPY).
    Formula: round(amount_minor / 10^from_dp * rate * 10^to_dp)
    Use Decimal arithmetic throughout."""
```

All other files import from here. This is the single source of truth for currency decimal places.

---

## Step 1: Update `convert_amount` in `exchange.py`

**File:** `backend/ninjatab/currencies/exchange.py`

Change signature from `(Decimal, str, str) -> Decimal` to `(int, str, str) -> int`.

```python
def convert_amount(amount: int, from_currency: str, to_currency: str, as_of_date=None) -> int:
    if from_currency == to_currency:
        return amount  # already int minor units, no quantize needed
    rate = get_latest_exchange_rate(from_currency, to_currency, as_of_date)
    return minor_to_minor(amount, from_currency, to_currency, rate)
```

Note: `get_latest_exchange_rate` still returns `Decimal` (it's an exchange ratio, not a currency amount — leave it unchanged).

---

## Step 2: Model Field Changes

**File:** `backend/ninjatab/tabs/models.py`

| Model | Field | Old | New |
|---|---|---|---|
| `Tab` | `settlement_currency_settled_total` | `DecimalField(max_digits=12, decimal_places=2, null=True)` | `IntegerField(null=True, blank=True)` |
| `LineItem` | `value` | `DecimalField(max_digits=10, decimal_places=2)` | `IntegerField()` |
| `PersonLineItemClaim` | `split_value` | `DecimalField(max_digits=10, decimal_places=2, null=True)` | `IntegerField(null=True, blank=True)` |
| `PersonLineItemClaim` | `calculated_amount` | `DecimalField(max_digits=10, decimal_places=2, null=True)` | `IntegerField(null=True, blank=True)` |
| `PersonLineItemClaim` | `settlement_amount` | `DecimalField(max_digits=10, decimal_places=2, null=True)` | `IntegerField(null=True, blank=True)` |
| `Settlement` | `amount` | `DecimalField(max_digits=10, decimal_places=2)` | `IntegerField()` |

**`split_value` semantics:**
- `SHARES` mode: stores a whole-number share count (e.g. `2`). Already integer-like, cast directly.
- `VALUE` mode: stores a currency amount — now stored as minor units (e.g. `1050` = $10.50).

Update `help_text`:
```python
split_value = models.IntegerField(
    null=True, blank=True,
    help_text="Number of shares (SHARES mode) or minor currency units (VALUE mode)"
)
```

**`Bill.total_amount` property:** Returns `sum(item.value ...)` — sum of ints is int. No logic change needed.

---

## Step 3: Django Migration

**File:** `backend/ninjatab/tabs/migrations/0010_currency_fields_to_integer.py`

Since existing data will be wiped (as noted by the user), the data migration can simply truncate/reset affected tables rather than converting values. However, a proper reversible migration is still recommended:

```python
operations = [
    migrations.RunPython(convert_decimal_to_minor_units, reverse_code=convert_minor_units_to_decimal),
    migrations.AlterField(model_name='lineitem', name='value', field=models.IntegerField()),
    migrations.AlterField(model_name='personlineitemclaim', name='split_value', field=models.IntegerField(null=True, blank=True, ...)),
    migrations.AlterField(model_name='personlineitemclaim', name='calculated_amount', field=models.IntegerField(null=True, blank=True, ...)),
    migrations.AlterField(model_name='personlineitemclaim', name='settlement_amount', field=models.IntegerField(null=True, blank=True, ...)),
    migrations.AlterField(model_name='settlement', name='amount', field=models.IntegerField()),
    migrations.AlterField(model_name='tab', name='settlement_currency_settled_total', field=models.IntegerField(null=True, blank=True, ...)),
]
```

**`RunPython` data migration logic:**
- `LineItem.value`: `int(round(float(value) * 10 ** get_decimal_places(bill.currency)))`
- `PersonLineItemClaim.split_value` when `split_type == 'shares'`: `int(round(float(split_value)))` (no currency scaling)
- `PersonLineItemClaim.split_value` when `split_type == 'value'`: scale by bill's currency decimal places
- `PersonLineItemClaim.calculated_amount`: scale by bill's currency
- `PersonLineItemClaim.settlement_amount`: scale by tab's `settlement_currency`
- `Settlement.amount`: scale by `currency` field
- `Tab.settlement_currency_settled_total`: scale by `settlement_currency`

Wrap in a single transaction. Process with `iterator(chunk_size=500)` for large tables.

---

## Step 4: Update `_create_person_claims` Arithmetic in `api.py`

**File:** `backend/ninjatab/tabs/api.py`

**SHARES path:**
```python
# line_item.value is int, split_value is int (share count), total_shares is int
calculated_amount = round(line_item.value * person_split.split_value / total_shares)
# result is int (minor units) — remove .quantize() call entirely
```

`total_shares` is now `sum(ps.split_value ...)` of ints — no Decimal needed.

**VALUE path:**
```python
calculated_amount = person_split.split_value  # already int minor units — no change
```

**Fallback:** Replace `Decimal(0)` with `0`.

**`close_tab` function:**
```python
total = 0  # int
for bill in bills:
    bill_total = sum((li.value or 0) for li in bill.line_items.all())  # int
    if bill.currency != tab.settlement_currency:
        bill_total = convert_amount(bill_total, bill.currency, tab.settlement_currency)  # int
    total += bill_total
# total is int minor units
```

**`retrieve_tab` Coalesce annotations:**
```python
from django.db.models import IntegerField
user_owes=Coalesce(Subquery(user_owes_sq), 0, output_field=IntegerField()),
user_owed=Coalesce(Subquery(user_owed_sq), 0, output_field=IntegerField()),
```

**`get_tab_person_totals`:** Replace `Decimal('0')` fallback with `0`.

**`update_tab` / `update_bill` currency change:** `convert_amount(claim.calculated_amount, ...)` already takes/returns int — no structural change needed.

---

## Step 5: Update `simp.py`

**File:** `backend/ninjatab/tabs/simp.py`

- Change `Balance.balance` and `Transaction.amount` type annotations from `Decimal` to `int`.
- Replace `Decimal('0')` literals with `0`.
- Update threshold comparisons:

```python
# Old
if abs(bals[creditor_id]) < Decimal('0.01') ...
# New: in minor units, 1 is the smallest unit (1 cent)
if abs(bals[creditor_id]) < 1 ...
```

- `calculate_tab_balances`: `amount = claim.calculated_amount or 0` (int). `payer_total` starts as `0`. `balances_by_person` values are int.
- Remove `from decimal import Decimal` import entirely.

---

## Step 6: Update `schemas.py` — Serializer Boundary

**File:** `backend/ninjatab/tabs/schemas.py`

### 6a. Input schemas — accept int from clients

Change currency value fields to `int`:
- `LineItemCreateSchema.value: int`
- `PersonSplitCreateSchema.split_value: Optional[int] = None`
- `PersonSplitSubmitSchema.split_value: Optional[int] = None`

The `validate_person_splits` validator compares ints — no Decimal arithmetic, no change to logic.

### 6b. Output schemas — expose `*_display` alongside raw int

Add a `display_*` computed Decimal field alongside each raw int currency field. Use `currency_utils.minor_to_decimal` for all conversions. The currency code must be available in each schema's context (passed via the parent's `model_validator`).

**Pattern for passing currency down the schema tree:**

`BillSchema` validator passes `currency: bill.currency` into each line item dict. `LineItemSchema` accepts `currency: str` and passes it into each claim dict. `PersonLineItemClaimSchema` accepts `currency: str`.

**`PersonLineItemClaimSchema`** adds:
- `split_value: Optional[int]` (raw)
- `calculated_amount: Optional[int]` (raw)
- `calculated_amount_display: Optional[Decimal] = None` (from `minor_to_decimal(calculated_amount, currency)`)
- `split_value_display: Optional[Decimal] = None` (only non-null when `split_type == VALUE`)

**`LineItemSchema`** adds:
- `value: int` (raw)
- `value_display: Optional[Decimal]` (from `minor_to_decimal(value, currency)`)

**`BillSchema`** adds:
- `total_amount: int` (raw)
- `total_amount_display: Optional[Decimal]`

**`BillListSchema`** adds:
- `total_amount: int` (raw)
- `total_amount_display: Optional[Decimal]`

**`SettlementSchema`** adds:
- `amount: int` (raw)
- `amount_display: Decimal` (from `minor_to_decimal(amount, currency)`)

**`TabSchema`** changes:
- `settlement_currency_settled_total: Optional[int]` + `settlement_currency_settled_total_display: Optional[Decimal]`
- `totals_by_currency: dict[str, int]` (was `dict[str, Decimal]`) + `totals_by_currency_display: dict[str, Decimal]`
- `group_spend: Optional[int]` + `group_spend_display: Optional[Decimal]`
- `user_owes: int` (default `0`) + `user_owes_display: Decimal`
- `user_owed: int` (default `0`) + `user_owed_display: Decimal`

**`PersonSpendingTotalSchema`** adds:
- `total: int` + `total_display: Decimal`

### 6c. `TabSchema.extract_people` totals calculation

Replace `Decimal('0')` fallbacks with `0`. `group_spend` and `totals_by_currency` values are int. `convert_amount` returns int. Remove `from decimal import Decimal` from inside the validator.

---

## Step 7: Verify Receipt Service

**File:** `backend/ninjatab/tabs/receipt_service.py`

Confirm that any parsed receipt values returned to the client or stored in the DB are in the correct format. If `scan_receipt` returns line-item values as floats/Decimals, they must be converted to int minor units before being stored via `create_bill`. Likely handled at the API endpoint level rather than in this service.

---

## Step 8: Clean Up Imports

| File | Action |
|---|---|
| `api.py` | Remove `Decimal` import if no remaining uses; add `IntegerField` import |
| `simp.py` | Remove `from decimal import Decimal` entirely |
| `exchange.py` | Keep `Decimal` for `get_latest_exchange_rate` return type; no change |
| `schemas.py` | Keep `Decimal` for `*_display` field types; import `currency_utils` |

---

## Execution Order

1. **Step 0** — Create `currency_utils.py` (foundational, no dependencies)
2. **Step 1** — Update `exchange.py` (depends on Step 0)
3. **Step 2** — Update `models.py`
4. **Step 3** — Write and run migration (depends on Step 2)
5. **Steps 4, 5, 6** — Update `api.py`, `simp.py`, `schemas.py` (can be done in parallel, all depend on Steps 0–2)
6. **Step 7** — Verify receipt service
7. **Step 8** — Final import cleanup

---

## Edge Cases and Pitfalls

1. **JPY/HUF zero-decimal currencies:** `decimal_to_minor(Decimal('1500'), 'JPY')` → `1500`, not `150000`. Always use `CURRENCY_DECIMAL_PLACES` — never hard-code `* 100`.

2. **`split_value` in SHARES mode:** The data migration must check `split_type` before scaling. For SHARES rows, cast directly to int (no currency scaling).

3. **SHARES arithmetic:** `round(line_item.value * split_value / total_shares)` — Python 3 `/` on ints returns float, `round()` gives int. This is correct.

4. **`simp()` threshold:** `< Decimal('0.01')` (1 cent major) becomes `< 1` (1 minor unit) — semantically identical.

5. **`Coalesce` output_field:** Must change from `DecimalField()` to `IntegerField()` or Django ORM will type-mismatch.

6. **`ExchangeRate.rate` stays `Decimal`:** It's a ratio (e.g. `1.23456`), not a currency amount. Do not change.

7. **`total_shares` stays int:** Sum of integer share counts — no Decimal needed anywhere in the SHARES calculation path.

8. **`TabSchema` default values:** `user_owes: int = 0`, `user_owed: int = 0` (was `Decimal('0')`).

9. **Data migration atomicity:** Wrap `RunPython` in a transaction. Fail fast if any row can't be converted.

---

## Summary of Files

| File | Change |
|---|---|
| `tabs/currency_utils.py` | **NEW** — `minor_to_decimal`, `decimal_to_minor`, `minor_to_minor`, `CURRENCY_DECIMAL_PLACES` |
| `currencies/exchange.py` | `convert_amount`: `int → int`; import `minor_to_minor` |
| `tabs/models.py` | 6 fields → `IntegerField`; `split_value` help_text updated |
| `tabs/migrations/0010_currency_fields_to_integer.py` | **NEW** — `RunPython` data migration + 6 `AlterField` ops |
| `tabs/api.py` | `_create_person_claims` arithmetic; `close_tab` totals; `Coalesce` output_field |
| `tabs/simp.py` | Type annotations; threshold `< 1`; remove `Decimal` import |
| `tabs/schemas.py` | Input `int`; output `int` + `*_display: Decimal`; import `currency_utils` |
