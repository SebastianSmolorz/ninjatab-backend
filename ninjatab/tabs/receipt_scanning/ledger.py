"""Reconcile a receipt as a ledger of items, charges and discounts.

The original post-processing defines `items_total` as the sum of `items`, so the
only way to make a receipt's arithmetic close is to push charges *into* the item
list. Doing that destroys the model's own statement of what each row was — it
said "this is a service charge" by putting the amount in `service_charge` — and
a name regex then has to guess the category back from three words of text.

This module keeps the three roles separate and reconciles across all of them:

    sum(item net totals) + sum(additive charges) == receipt_total

where an item's net total already accounts for its discounts. Which charges are
*additive* is decided arithmetically rather than by wording: a receipt whose
prices already include VAT reaches its total without the tax line, and one that
adds service on top does not. Provenance travels with each charge, so the
category the client splits on is the model's classification, not a rediscovery.

The reconciled result keeps the three roles separate: `items` (things someone
ordered, claimable by a person) and `adjustments` (receipt-level charges, each
typed and carrying the split mode it should default to). That is what the v2
endpoint returns.

`flatten_for_v1` collapses it back into the single `items` list the shipped
mobile client understands - one flat list where charge rows carry a `category` -
and is applied only on the v1 endpoint.
"""

import logging
import re
import unicodedata
from itertools import combinations
from typing import Optional

from .postprocess import (
    _annotation_decimals,
    _annotation_tolerance,
    _categorize_item_name,
    _collapse_redundant_translations,
    _normalize_amounts_in_annotation,
    _synthesize_total_only_item,
    _to_float,
    SUPPORTED_CURRENCY_CODES,
)

logger = logging.getLogger("app")

# Where a charge came from in the model's own output, and the category the
# client uses to decide the split mode. "fee" has no client category of its own;
# it redistributes like a service charge.
CHARGE_SOURCES = (
    ("tax", "tax"),
    ("tip", "tip"),
    ("service_charge", "service"),
)

# Charges are searched over subsets to find which combination is additive; this
# bounds that search. Receipts do not carry many distinct charge lines.
MAX_CHARGES_CONSIDERED = 12


# The model's `kind` values, mapped onto the categories the client splits on.
#
# A discount reaches the client as its own negative row beneath the item it
# reduces. It maps to "service" so the client redistributes it proportionally
# rather than offering a negative line for someone to be asked to split evenly.
_KIND_TO_CATEGORY = {
    "item": "item",
    "tax": "tax",
    "tip": "tip",
    "service": "service",
    "discount": "service",
}

# How each adjustment type is split when nobody has said otherwise. A fee is a
# flat cost everyone shares equally; tax, tip, service and discounts scale with
# what each person actually claimed.
# ponytail: a literal map, not config. Change it here if a type moves.
_SPLIT_BY_TYPE = {
    "fee": "even",
    "tax": "proportional",
    "tip": "proportional",
    "service": "proportional",
    "discount": "proportional",
}


# Tip is its own thing in the client, with its own selector and its own
# exclusion from the tip base, so it is worth recognising by name as well as by
# provenance. Tax deliberately is not: it is populated only where the *model*
# categorised the amount as tax, never from a word we matched ourselves.
_TIP_WORDS = (
    "tip", "tips", "gratuity", "propina", "trinkgeld", "pourboire", "mancia",
)
_TIP_RE = re.compile(
    r"(?<![\w])(?:" + "|".join(sorted(_TIP_WORDS, key=len, reverse=True)) + r")(?![\w])",
    re.IGNORECASE,
)


def categorise_charge(
    name: Optional[str], translated: Optional[str] = None, amount: float = 0.0
) -> str:
    """Categorise an `other_charges` entry, which the model left unclassified.

    Tip is identified; everything else becomes an ordinary line item, split
    evenly. A delivery, handling or card fee is something everyone shares
    equally, so that is a reasonable default, and calling it tax would be a
    claim the model never made.

    A negative amount is the exception: a credit must never become a claimable
    even-split row, so it stays a charge the client redistributes.
    """
    if amount < 0:
        return "service"
    for text in (translated, name):
        folded = _fold(text)
        if folded and _TIP_RE.search(folded):
            return "tip"
    return "item"


def demote_duplicate_taxes(rows: list[dict]) -> int:
    """Leave at most one row categorised as tax, keeping the largest.

    The client's tax selector reads the *first* tax row and, on edit, deletes
    every tax row and writes back a single one - so a second tax row is both
    invisible and destructive to the total. Keeping the largest means the tax
    the user sees is a figure actually printed on the receipt; the remainder
    stays in the bill as an ordinary line rather than being silently dropped.
    """
    taxes = [i for i, r in enumerate(rows) if r.get("category") == "tax"]
    if len(taxes) < 2:
        return 0
    keep = max(taxes, key=lambda i: _to_float(rows[i].get("total")) or 0.0)
    demoted = 0
    for i in taxes:
        if i != keep:
            rows[i]["category"] = "item"
            demoted += 1
    return demoted


def _fold(text: Optional[str]) -> str:
    """Accent-insensitive lowercase, so 'Serviço' and 'ÁFA' match."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def expand_discount_rows(items: list[dict], decimals: int = 2) -> int:
    """Give each item's discounts their own negative row beneath that item.

    A receipt prints "Crisps 3.00 / 3 for 2  -1.00", and the client shows the
    rows in the order it receives them, so emitting the discount as the next row
    reproduces the receipt without the client needing to know what an adjustment
    is.

    The model reports `total` as the price actually paid, so the item is grossed
    back up by the discount it already absorbed and the discount follows it. The
    pair still sums to what was paid, leaving every total in the bill unchanged:

        3.00 (gross) + -1.00 (discount) == 2.00 (paid)

    A `is_new_price` adjustment is left folded into the total, because a
    "was 5.00, now 3.00" line records the new price rather than a delta, so
    there is no reduction to show without inventing one.
    """
    expanded = 0
    out: list[dict] = []
    for item in items:
        deltas = _expandable_adjustments(item)
        if not deltas:
            out.append(item)
            continue

        paid = _to_float(item.get("total")) or 0.0
        reduction = sum(_to_float(a["amount"]) or 0.0 for a in deltas)
        gross = round(paid - reduction, 6)
        # A row that grosses up to nothing was only ever carrying the discount
        # (the model sometimes emits a coupon line that also lists itself as its
        # own adjustment). Emitting it would leave a 0.00 item beside the real
        # discount row.
        if gross != 0.0:
            row = dict(item)
            row["total"] = f"{gross:.{decimals}f}"
            out.append(row)
        for adjustment in deltas:
            name = adjustment.get("name") or "Discount"
            out.append({
                "name": name,
                "translated_name": name,
                "total": f"{_to_float(adjustment['amount']) or 0.0:.{decimals}f}",
                "kind": "discount",
            })
            expanded += 1
    items[:] = out
    return expanded


def row_category(item: dict) -> str:
    """The category a row should carry.

    Prefers the model's own `kind`, which was decided while the row's position
    and alignment on the receipt were still visible. Falls back to matching the
    name only when the model did not classify — the name is all that survives
    extraction, so it is the weaker signal and is used as such.
    """
    # A row worth less than nothing is a credit, whatever it is called. Nobody
    # ordered it, so it must never be offered as an evenly-split line; treating
    # it as a charge lets the client spread it across the bill instead.
    total = _to_float(item.get("total"))
    if total is not None and total < 0:
        return "service"

    kind = (item.get("kind") or "").strip().lower()
    if kind in _KIND_TO_CATEGORY:
        return _KIND_TO_CATEGORY[kind]
    return _categorize_item_name(item.get("name") or item.get("translated_name"))


def item_net_total(item: dict) -> float:
    """An item's contribution to the bill.

    This is simply the line total, because the prompt defines `total` as "the
    final price paid for that line item" — a discount printed against the item
    is already reflected in it. Subtracting the adjustment as well would take
    the reduction off twice, which measurably wrecked item totals on receipts
    where the model reported both.

    Adjustments are not applied here at all: `expand_discount_rows` has already
    turned each one into its own row, grossing this total back up to match, so
    the reduction is counted once — by that row.
    """
    return _to_float(item.get("total")) or 0.0


def items_net_sum(items: list[dict], decimals: int) -> float:
    return round(sum(item_net_total(i) for i in items), decimals)


def adjustment_type(
    category: str, amount: float, model_type: Optional[str] = None
) -> str:
    """The v2 type for a charge: tax | tip | service | fee | discount.

    The model's own classification wins where it gave one — it could see where
    the line sat on the receipt, which is more than a name survives. Otherwise
    the type is derived: a credit is a discount, a v1 category carries straight
    across, and anything left over is a fee (a delivery, booking or card charge
    everyone shares equally).
    """
    if model_type in _SPLIT_BY_TYPE:
        return model_type
    if amount < 0:
        return "discount"
    if category in ("tax", "tip", "service"):
        return category
    return "fee"


def collect_charges(annotation: dict) -> list[dict]:
    """Charges the model reported, each tagged with where it came from.

    Provenance is the point: `category` (v1) and `type` (v2) are what the model
    classified the amount as, not what a regex later guesses from its name.
    """
    charges: list[dict] = []
    for key, category in CHARGE_SOURCES:
        amount = _to_float(annotation.get(key))
        if amount is None:
            continue
        label = key.replace("_", " ").title()
        charges.append(_charge(label, label, amount, category, key))
    for charge in annotation.get("other_charges") or []:
        amount = _to_float(charge.get("amount"))
        if amount is None:
            continue
        name = charge.get("name") or "Charge"
        charges.append(_charge(
            name,
            charge.get("translated_name") or name,
            amount,
            # v1's category is left exactly as it was: an unrecognised fee still
            # reaches the old client as an evenly-split line item.
            categorise_charge(name, charge.get("translated_name"), amount),
            "other_charges",
            model_type=charge.get("type"),
        ))
    return charges


def _charge(
    name: str, translated: str, amount: float, category: str, source: str,
    model_type: Optional[str] = None,
) -> dict:
    kind = adjustment_type(category, amount, model_type)
    return {
        "name": name,
        "translated_name": translated,
        "amount": amount,
        "category": category,          # v1: how the flat client splits the row
        "type": kind,                  # v2: what the charge actually is
        "split": _SPLIT_BY_TYPE[kind],
        "source": source,
    }


def _plausible_charges(subset: list[dict], items_subtotal: float) -> bool:
    """Whether treating `subset` as additive describes a real receipt.

    A charge sits *on top of* what was ordered, so it cannot be worth more than
    the order itself: tax, service and tip together stay well under the item
    subtotal on any real bill. Without this guard, a parse that misfiles the
    whole bill as one "service fee" reconciles perfectly with an empty item
    list — and, because reconciling candidates are preferred during consensus,
    that degenerate reading would win. Reconciliation must not be reachable by
    emptying the receipt.
    """
    if not subset:
        return True
    if items_subtotal <= 0:
        return False
    return sum(c["amount"] for c in subset) <= items_subtotal


def select_additive_charges(
    items: list[dict], charges: list[dict], receipt_total: Optional[float],
    decimals: int, tolerance: float,
) -> tuple[list[dict], str]:
    """Choose the subset of charges that must be added to the items to reach
    receipt_total. Returns (additive charges, how it was decided).

    Deciding this arithmetically is what separates a VAT-inclusive receipt from
    one that adds service on top, without needing to know the tax rules of the
    country it came from.
    """
    if receipt_total is None:
        return [], "no_receipt_total"

    base = items_net_sum(items, decimals)
    if abs(base - receipt_total) < tolerance:
        return [], "items_alone"  # prices already include everything

    considered = charges[:MAX_CHARGES_CONSIDERED]
    for size in range(1, len(considered) + 1):
        for combo in combinations(range(len(considered)), size):
            subset = [considered[i] for i in combo]
            total = round(base + sum(c["amount"] for c in subset), decimals)
            if abs(total - receipt_total) < tolerance and _plausible_charges(subset, base):
                return subset, "subset_reconciled"

    # Nothing closes the gap. Add nothing: a charge that cannot be shown to be
    # additive may well be inclusive (VAT already inside the prices), and adding
    # it would double-count the bill. Leaving the receipt visibly unreconciled is
    # the honest outcome, and the client already flags that for review.
    #
    # Measured: surfacing these charges anyway lifts charge F1 by 0.034 but costs
    # 0.010 of item-total F1 and two cases of run-to-run stability. Item totals
    # outrank charges, so it is not a trade worth taking.
    return [], "unreconciled_none_added"


def reconcile_ledger(annotation: dict, default_currency: str) -> dict:
    """Post-process one parsed annotation into a reconciled ledger, in place.

    Drop-in replacement for `standard_post_process`: same call signature, same
    outgoing annotation shape, different (and checkable) arithmetic.
    """
    metrics: dict = {
        "annotation_present": True,
        "currency_source": None,
        "currency_code": None,
        "currency_decimals": None,
        "items_count": 0,
        "items_total": None,
        "ai_items_total": None,
        "receipt_total": None,
        "items_match_receipt_total": None,
        "items_receipt_gap": None,
        "charges_count": 0,
        "charges_additive_count": 0,
        "charge_selection": None,
        "discounts_count": 0,
        "synthesized_total_only_item": False,
        "has_tax": False,
        "has_tip": False,
        "has_service_charge": False,
        "other_charges_count": 0,
    }

    raw_code = (annotation.get("currency_code") or "").strip().upper()
    if not raw_code:
        annotation["currency_code"] = default_currency
        metrics["currency_source"] = "fallback_missing"
    elif raw_code not in SUPPORTED_CURRENCY_CODES:
        logger.warning(
            "Mistral OCR returned unsupported currency_code %r; falling back to %s",
            raw_code, default_currency,
        )
        annotation["currency_code"] = default_currency
        metrics["currency_source"] = "fallback_unsupported"
        metrics["currency_unsupported_raw"] = raw_code
    else:
        annotation["currency_code"] = raw_code
        metrics["currency_source"] = "model"
    metrics["currency_code"] = annotation["currency_code"]

    decimals = _annotation_decimals(annotation)
    tolerance = _annotation_tolerance(annotation)
    metrics["currency_decimals"] = decimals

    _normalize_amounts_in_annotation(annotation)
    _normalize_adjustment_amounts(annotation, decimals)
    annotation["ai_items_total"] = annotation.pop("items_total", None)
    metrics["synthesized_total_only_item"] = _synthesize_total_only_item(annotation)

    metrics["has_tax"] = annotation.get("tax") is not None
    metrics["has_tip"] = annotation.get("tip") is not None
    metrics["has_service_charge"] = annotation.get("service_charge") is not None
    metrics["other_charges_count"] = len(annotation.get("other_charges") or [])

    items = list(annotation.get("items") or [])
    metrics["discount_rows_expanded"] = sum(
        1 for i in items for a in _expandable_adjustments(i)
    )
    charges = collect_charges(annotation)
    receipt_total = _to_float(annotation.get("receipt_total"))
    metrics["charges_count"] = len(charges)
    metrics["receipt_total"] = receipt_total
    metrics["discounts_count"] = sum(len(i.get("adjustments") or []) for i in items)

    additive, how = select_additive_charges(
        items, charges, receipt_total, decimals, tolerance
    )
    metrics["charge_selection"] = how
    metrics["charges_additive_count"] = len(additive)

    # Emit the ledger: what was ordered in `items`, everything charged on top of
    # it in `adjustments`. Rows the model filed as items but classified as a
    # charge move across - it told us what they were, so they belong with the
    # other charges rather than being offered up for someone to claim.
    categories = [row_category(i) for i in items]
    # Classifying every row as a charge would leave nothing to split, which is
    # never a real receipt - a bill is things someone ordered, plus charges on
    # top. When the model's `kind` would empty the item list, disregard it and
    # treat the rows as items; a wrong split mode is a far smaller error than an
    # empty bill.
    if (
        items
        and all(c != "item" for c in categories)
        # Only when there is a positive bill to rescue. A set of rows that sums
        # to nothing or less is not a receipt whose items went missing, and
        # forcing credits back to "item" would offer them up to be split.
        and items_net_sum(items, decimals) > 0
    ):
        logger.info(
            "Ignoring row kinds: every row classified as a charge, leaving no items"
        )
        categories = ["item"] * len(items)
        metrics["kinds_overridden"] = True

    item_rows: list[dict] = []
    promoted: list[dict] = []
    for item, category in zip(items, categories):
        net = item_net_total(item)
        row = dict(item)
        row["total"] = f"{net:.{decimals}f}"
        row["category"] = category
        if category == "item":
            item_rows.append(row)
        else:
            name = row.get("name") or "Charge"
            promoted.append(_charge(
                name, row.get("translated_name") or name, net, category, "item_row",
            ))

    adjustments = [
        {
            "name": c["name"],
            "translated_name": c["translated_name"],
            "amount": f"{c['amount']:.{decimals}f}",
            "type": c["type"],
            "split": c["split"],
            "source": c["source"],
            # How the pre-v2 client splits this row. Carried so `flatten_for_v1`
            # is exactly lossless rather than re-deriving a category and quietly
            # changing behaviour for the shipped app.
            # ponytail: delete with flatten_for_v1 once v1 is retired.
            "legacy_category": c["category"],
        }
        for c in promoted + additive
    ]

    annotation["items"] = item_rows
    annotation["adjustments"] = adjustments
    annotation["charges"] = [
        {**c, "amount": f"{c['amount']:.{decimals}f}", "additive": c in additive}
        for c in charges
    ]
    _collapse_redundant_translations(annotation)

    items_total = round(sum(_to_float(r.get("total")) or 0.0 for r in item_rows), decimals)
    adjustments_total = round(
        sum(_to_float(a["amount"]) or 0.0 for a in adjustments), decimals
    )
    total = round(items_total + adjustments_total, decimals)
    annotation["items_total"] = items_total
    annotation["adjustments_total"] = adjustments_total
    annotation["grand_total"] = total

    metrics["duplicate_taxes_demoted"] = max(
        0, sum(1 for a in adjustments if a["type"] == "tax") - 1
    )
    metrics["items_count"] = len(item_rows) + len(adjustments)
    metrics["items_total"] = total
    metrics["adjustments_count"] = len(adjustments)
    metrics["promoted_item_rows"] = len(promoted)
    metrics["ai_items_total"] = _to_float(annotation.get("ai_items_total"))
    if receipt_total is not None:
        gap = round(total - receipt_total, 6)
        metrics["items_receipt_gap"] = gap
        metrics["items_match_receipt_total"] = abs(gap) < tolerance
        annotation["totals_reconciled"] = metrics["items_match_receipt_total"]
    return metrics


def flatten_for_v1(annotation: Optional[dict]) -> Optional[dict]:
    """Collapse a reconciled ledger back into the single flat `items` list the
    shipped mobile client understands, in place.

    The client has no concept of an adjustment: a charge reaches it as another
    row carrying a `category` that decides its split mode, an item-level discount
    reaches it as a negative row beneath a grossed-up item, and only one tax row
    may exist because its tax editor rewrites every one it finds.

    Applied only on the v1 endpoint. A v2 client gets the ledger untouched.
    """
    if not annotation or "adjustments" not in annotation:
        return annotation

    decimals = _annotation_decimals(annotation)
    rows = [dict(i) for i in annotation.get("items") or []]
    expand_discount_rows(rows, decimals)
    for row in rows:
        # Rows created by the expansion have no category yet; a credit is spread
        # across the bill rather than offered to someone to claim.
        row.setdefault("category", row_category(row))

    for adjustment in annotation.pop("adjustments"):
        rows.append({
            "name": adjustment["name"],
            "translated_name": adjustment["translated_name"],
            "total": adjustment["amount"],
            "category": adjustment["legacy_category"],
            "charge_source": adjustment["source"],
        })

    demote_duplicate_taxes(rows)
    annotation["items"] = rows
    annotation["items_total"] = annotation.pop("grand_total", None)
    annotation.pop("adjustments_total", None)
    return annotation


def _expandable_adjustments(item: dict) -> list[dict]:
    """An item's discounts that become their own row: a real reduction, not a
    zero and not a "was/now" line that restates the price."""
    return [
        a for a in (item.get("adjustments") or [])
        if not a.get("is_new_price") and (_to_float(a.get("amount")) or 0.0) != 0.0
    ]


def _normalize_adjustment_amounts(annotation: dict, decimals: int) -> None:
    from .postprocess import _normalize_amount_str

    for item in annotation.get("items") or []:
        for adjustment in item.get("adjustments") or []:
            if "amount" in adjustment:
                adjustment["amount"] = _normalize_amount_str(
                    adjustment["amount"], decimals
                )
