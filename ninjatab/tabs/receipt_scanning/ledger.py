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
    _printed_values,
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
    # Gratuity, tip and service charge are the same thing to a diner: money for
    # the staff, on top of what was ordered. Treating them as one keeps the
    # client's tip control meaningful on a receipt that prints "12.5% Service
    # Charge" instead of "Tip".
    ("service_charge", "tip"),
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
    # Not a tip. `kind: "service"` is the model's catch-all for "this row is not
    # something anyone ordered", and it lands on cover charges (an Italian
    # coperto is a per-head charge, not gratuity), delivery, booking and card
    # fees alike. A real service charge arrives in the `service_charge` field,
    # which does map to tip - see CHARGE_SOURCES.
    # Measured: tip F1 0.7627 -> 0.8182, charges 0.7879 -> 0.8211. p1 -0.0022,
    # entirely one illegible receipt dropping 0.20 -> 0.00; the coperto case it
    # targets gained 0.04.
    "service": "item",
    # A discount is a line on the bill like any other, just a negative one - it
    # sits under the item it reduces, or at the bottom if it applies to the
    # whole receipt. The category marks it as not claimable: nobody ordered a
    # credit, so it is redistributed rather than offered up for someone to tab.
    "discount": "discount",
}

# Only two things leave the item list, because only two have a control of their
# own in the app: tax added on top of the prices, and tip. Discounts stay in the
# list as negative rows, positioned where the receipt printed them.
# ponytail: two special cases beat a five-way taxonomy nothing measures. Widen
# it when `charge_type_accuracy` shows a wider one would pay for itself.
_ADJUSTMENT_TYPES = ("tax", "tip")

_SPLIT_BY_TYPE = {
    "tax": "proportional",
    "tip": "proportional",
    "discount": "proportional",
}


# Tip is its own thing in the client, with its own selector and its own
# exclusion from the tip base, so it is worth recognising by name as well as by
# provenance. Tax deliberately is not: it is populated only where the *model*
# categorised the amount as tax, never from a word we matched ourselves.
# ponytail: no "service charge" here on purpose. Across 66 labelled receipts
# every service charge arrived in the `service_charge` field, never in
# `other_charges`, so matching the phrase by name would be speculative - and
# broad lexical matching is what regressed the last two times it was tried.
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
    even-split row, so it is marked as a discount and redistributed.
    """
    if amount < 0:
        return "discount"
    for text in (translated, name):
        folded = _fold(text)
        if folded and _TIP_RE.search(folded):
            return "tip"
    return "item"


# Categories the pre-v2 client shows exactly one control for. Its tax selector
# (tax_selector.dart:52) and tip selector (tip_selector.dart:58) both read the
# *first* matching row and, on edit, delete every matching row and write back a
# single one - so a second row of the same category is invisible and destructive
# to the total. v2 has no such limit; this is a property of the old client only.
_SINGLETON_CATEGORIES = ("tax", "tip")


def demote_duplicate_charges(rows: list[dict]) -> int:
    """Leave at most one row per singleton category, keeping the largest.

    Keeping the largest means the figure the user sees is one actually printed
    on the receipt; the remainder stays in the bill as an ordinary line rather
    than being silently dropped. Returns how many rows were demoted.
    """
    demoted = 0
    for category in _SINGLETON_CATEGORIES:
        matches = [i for i, r in enumerate(rows) if r.get("category") == category]
        if len(matches) < 2:
            continue
        keep = max(matches, key=lambda i: _to_float(rows[i].get("total")) or 0.0)
        for i in matches:
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


def receipt_total_of(annotation: dict) -> Optional[float]:
    return _to_float(annotation.get("receipt_total"))


def unmultiply_line_totals(
    items: list[dict], receipt_total: Optional[float], tolerance: float, decimals: int = 2
) -> int:
    """Undo a quantity multiplication the receipt's own total says was wrong.

    The model sometimes reads a printed *line total* as a unit price and
    multiplies it by the quantity: `4 BIRRA IPA  $ 32.000,00` came back as
    128,000 against a bill whose total was 137,000.

    The tell is that the emitted total is absent from `receipt_line_text` while
    `price_per_quantity` is present. That alone is not enough to act on —
    `2 x £4.99 -> 9.98` looks identical and is correct — so the row is rewritten
    only when doing so closes the gap between the items and `receipt_total`. The
    receipt's own arithmetic is the arbiter; nothing is picked out of the text.

    Measured over 430 scans: 7 rows rewritten, 7 of them wrong beforehand and
    none right. Ungated, the same test rewrites 46 rows and breaks 28 of them.
    """
    if receipt_total is None:
        return 0
    gap = sum(_to_float(i.get("total")) or 0.0 for i in items) - receipt_total
    if abs(gap) < tolerance:
        return 0

    for item in items:
        total = _to_float(item.get("total"))
        unit = _to_float(item.get("price_per_quantity"))
        quantity = item.get("quantity")
        if total is None or unit is None or not quantity or int(quantity) < 2:
            continue
        if abs(total - int(quantity) * unit) >= tolerance:
            continue  # the total is not a clean multiple, so this is not the case
        printed = _printed_values(item.get("receipt_line_text"))
        if any(abs(total - p) < tolerance for p in printed):
            continue  # the total is printed on the line: the model read it, not made it
        if not any(abs(unit - p) < tolerance for p in printed):
            continue  # neither figure is printed; there is nothing to fall back to
        if abs(gap - (total - unit)) < tolerance:
            item["total"] = f"{unit:.{decimals}f}"
            logger.info(
                "Un-multiplied a line total: %s x %s -> %s (closes a %s gap)",
                quantity, unit, unit, round(gap, decimals),
            )
            return 1
    return 0


def drop_restated_discount_summary(
    items: list[dict], charges: list[dict], receipt_total: Optional[float],
    tolerance: float, decimals: int = 2,
) -> int:
    """Remove a savings total that restates discounts already taken per item.

    Supermarket loyalty receipts print each saving under its item and then total
    them at the bottom — Tesco's "Savings -5.87", Sainsbury's "PROMOTIONS 5.50".
    The footer is a restatement, not a further deduction. The model reports it as
    a receipt-level discount *and* inflates each item by the saving it absorbed,
    so the two errors cancel: `select_additive_charges` finds that subtracting
    the footer closes the bill, which is arithmetically true and substantively
    wrong. The receipt reconciles to the penny on rows that are individually
    wrong, and no other check can see it.

    Because they cancel, neither half can be undone alone — dropping the footer
    by itself broke closure on all 17 scans where it was detected. The gross-ups
    are reversed and the footer discarded together, and only if the bill still
    closes afterwards.

    The footer is identified arithmetically rather than by wording: it is the
    negative charge equal to the sum of the discount rows already sitting in
    `items`. At least two of those are required, so that a single real discount
    reported once as a row and once as a charge is not mistaken for this.
    """
    if receipt_total is None:
        return 0
    categories = [row_category(i) for i in items]
    discounts = [i for i, c in enumerate(categories) if c == "discount"]
    if len(discounts) < 2:
        return 0
    taken = sum(_to_float(items[i].get("total")) or 0.0 for i in discounts)

    summary = next(
        (c for c in charges
         if (_to_float(c.get("amount")) or 0.0) < 0
         and abs((_to_float(c["amount"]) or 0.0) - taken) < tolerance),
        None,
    )
    if summary is None:
        return 0

    rebuilt: list[dict] = []
    for i, item in enumerate(items):
        total = _to_float(item.get("total"))
        following = categories[i + 1] if i + 1 < len(items) else None
        if categories[i] == "item" and following == "discount" and total is not None:
            reduction = _to_float(items[i + 1].get("total")) or 0.0
            printed = _printed_values(item.get("receipt_line_text"))
            # Grossed up exactly when the printed figure is the total *net* of
            # the discount sitting beneath it.
            if (not any(abs(total - p) < tolerance for p in printed)
                    and any(abs(total + reduction - p) < tolerance for p in printed)):
                item = dict(item)
                item["total"] = f"{total + reduction:.{decimals}f}"
        rebuilt.append(item)

    if abs(sum(_to_float(i.get("total")) or 0.0 for i in rebuilt) - receipt_total) >= tolerance:
        return 0  # the pair does not explain the bill; leave it alone
    logger.info(
        "Dropped a restated savings total of %s and ungrossed its items",
        round(_to_float(summary["amount"]) or 0.0, decimals),
    )
    items[:] = rebuilt
    charges.remove(summary)
    return 1


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
            # The reduction is the row that follows; keeping it here as well
            # would state it twice. Anything not expanded (a zero, or a
            # "was/now" line) stays attached.
            kept = [a for a in (item.get("adjustments") or []) if a not in deltas]
            if kept:
                row["adjustments"] = kept
            else:
                row.pop("adjustments", None)
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
        return "discount"

    kind = (item.get("kind") or "").strip().lower()
    if kind in _KIND_TO_CATEGORY:
        return _KIND_TO_CATEGORY[kind]
    category = _categorize_item_name(item.get("name") or item.get("translated_name"))
    # A service-sounding name is an ordinary line now, same as the model's own
    # "service" kind. Only tax and tip leave the item list.
    return "item" if category == "service" else category


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
    if category in ("tax", "tip"):
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
        "split": _SPLIT_BY_TYPE.get(kind, "even"),
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
    # Runs before the charge search below, so a corrected items sum decides which
    # charges are additive.
    metrics["unmultiplied_rows"] = unmultiply_line_totals(items, receipt_total_of(annotation), tolerance, decimals)
    # An item discount becomes its own negative row directly beneath its item,
    # which is where the receipt printed it. Sum-preserving, so it cannot change
    # which charges the arithmetic below finds to be additive.
    metrics["discount_rows_expanded"] = expand_discount_rows(items, decimals)
    # After expansion, so a saving the model attached to its item is a row here
    # and can be counted against the footer that restates it.

    charges = collect_charges(annotation)
    # Before the charge search: a savings footer restates discounts already in
    # `items`, and the search would otherwise accept it because the grossed-up
    # items make subtracting it close the bill.
    metrics["restated_summary_dropped"] = drop_restated_discount_summary(
        items, charges, _to_float(annotation.get("receipt_total")), tolerance, decimals
    )
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
        # Only the two categories with a control of their own leave the list. A
        # discount row stays put, where the receipt printed it.
        if category in _ADJUSTMENT_TYPES:
            name = row.get("name") or "Charge"
            promoted.append(_charge(
                name, row.get("translated_name") or name, net, category, "item_row",
            ))
        else:
            item_rows.append(row)

    # A charge with no control of its own is just another line on the bill, so
    # it joins the items. Discounts that apply to the whole receipt land here
    # too, as negative rows at the bottom.
    for charge in additive:
        if charge["type"] not in _ADJUSTMENT_TYPES:
            item_rows.append({
                "name": charge["name"],
                "translated_name": charge["translated_name"],
                "total": f"{charge['amount']:.{decimals}f}",
                "category": charge["category"],
                "charge_source": charge["source"],
            })

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
        if c["type"] in _ADJUSTMENT_TYPES
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

    # How many rows v1 will have to demote. Reported here so the metric exists
    # regardless of which endpoint served the scan.
    metrics["duplicate_charges_demoted"] = sum(
        max(0, sum(1 for a in adjustments if a["type"] == c) - 1)
        for c in _SINGLETON_CATEGORIES
    )
    metrics["discount_rows"] = sum(
        1 for i in item_rows if i.get("category") == "discount"
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

    rows = [dict(i) for i in annotation.get("items") or []]
    for row in rows:
        # The old client knows "service" as its non-claimable, redistributed
        # category; it has never heard of "discount".
        if row.get("category") == "discount":
            row["category"] = "service"

    for adjustment in annotation.pop("adjustments"):
        rows.append({
            "name": adjustment["name"],
            "translated_name": adjustment["translated_name"],
            "total": adjustment["amount"],
            "category": adjustment["legacy_category"],
            "charge_source": adjustment["source"],
        })

    demote_duplicate_charges(rows)
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
