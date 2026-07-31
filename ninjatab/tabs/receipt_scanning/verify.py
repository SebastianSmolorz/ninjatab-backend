"""Deterministic surplus-row repair for a post-processed receipt annotation.

The Mistral document annotation is a single sampled parse, and its most common
structural error is emitting *too many* line-item rows: a wrapped product
description becomes its own row carrying a copy of the price, a multi-buy
"2 x 0.79" qualifier becomes a row alongside the product it qualifies, or the
receipt's own subtotal is transcribed as an item. Each of these inflates
items_total while leaving receipt_total — which is printed once, in large type,
and is read reliably — correct.

That gives a purely arithmetic repair signal that needs no second model call and
no receipt-specific knowledge: when the rows overshoot the receipt total by
exactly the value of some small set of rows, those rows are duplicates of
information already counted, and removing them makes the bill self-consistent.

`standard_post_process` already performs a narrower version of this, but only
considers rows whose *name* matches a tax/fee/discount vocabulary. The failures
that survive it are duplicated **product** rows, which carry ordinary product
names and so are never candidates. This stage widens the candidate set to any
row, and compensates for the wider net by requiring the repair to land the sum
exactly on receipt_total and by preferring rows that are independently
suspicious (a duplicated value, or a name that continues its neighbour).

This module is a single pipeline stage that reasons only from general receipt
structure and arithmetic — never from a particular receipt, merchant, or
expected value.
"""

import logging
import re
from itertools import combinations
from typing import Optional

from .postprocess import (
    _annotation_decimals,
    _annotation_tolerance,
    _is_likely_non_contributing,
    _items_sum,
    _normalize_amount_str,
    _to_float,
    bill_total,
    recompute_bill_totals,
)

logger = logging.getLogger("app")

# A repair removes at most this many rows. Real duplication artefacts are one or
# two rows; searching every subset is exponential and would hang the scan on a
# long supermarket receipt.
# ponytail: bounded at 3 so C(n,3) stays cheap. Raise only if evaluation shows
# receipts that genuinely need deeper repairs.
MAX_DROPPED_ROWS = 3
# Beyond this many candidate rows, even the bounded search is not worth running:
# a receipt needing that much repair is not one we can fix confidently.
MAX_DROP_CANDIDATES = 40


# Any number appearing in the transcribed receipt text.
_NUMBER_RE = re.compile(r"-?\d[\d  .,]*\d|-?\d")


def markdown_amount_counts(markdown: str, decimals: int) -> dict[str, int]:
    """How many times each amount appears in the OCR text.

    The count is the point, not mere presence. A receipt that prints one price
    for a line cannot justify two rows claiming it, while two genuinely repeated
    products print their price twice — which is what separates a duplication
    artefact from a customer ordering the same thing twice.
    """
    counts: dict[str, int] = {}
    for raw in _NUMBER_RE.findall(markdown or ""):
        cleaned = raw.replace(" ", "").replace(" ", "").strip(".,")
        if not cleaned:
            continue
        value = _to_float(_normalize_amount_str(cleaned, decimals))
        if value is None:
            continue
        key = f"{abs(value):.{decimals}f}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def drop_unattested_duplicates(
    items: list[dict], markdown: str, decimals: int
) -> Optional[list[dict]]:
    """Remove rows claiming an amount more often than the receipt text prints it.

    Returns the surviving rows, or None when nothing is unattested. Rows are kept
    in order, so the first occurrences survive and the later copies go — a
    duplicate follows the line it was duplicated from.

    This only nominates rows; whether they are actually dropped is decided by
    the caller, which requires the resulting bill to reconcile exactly. The text
    is not reliable enough to act on alone: OCR sometimes mangles a price out of
    the transcription entirely, and on receipts that already add up this test
    would wrongly flag a quarter of them.
    """
    if not markdown or not items:
        return None
    counts = markdown_amount_counts(markdown, decimals)
    if not counts:
        return None

    seen: dict[str, int] = {}
    kept: list[dict] = []
    for item in items:
        value = _to_float(item.get("total"))
        if value is None:
            kept.append(item)
            continue
        key = f"{abs(value):.{decimals}f}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= counts.get(key, 0):
            kept.append(item)
    if len(kept) == len(items) or not kept:
        return None
    return kept


def _norm_name(item: dict) -> str:
    return (item.get("name") or item.get("translated_name") or "").strip().casefold()


def _suspicion(items: list[dict], index: int, decimals: int) -> int:
    """How likely a row is to be a duplication artefact rather than a product.

    Higher is more suspicious. Used only to order the search, so a genuine
    product is never dropped on suspicion alone — the arithmetic still has to
    come out exactly right.
    """
    item = items[index]
    score = 0
    value = _to_float(item.get("total"))
    name = _norm_name(item)

    # A value that another row also carries: the classic duplicated-price row.
    if value is not None:
        for other_index, other in enumerate(items):
            if other_index == index:
                continue
            other_value = _to_float(other.get("total"))
            if other_value is not None and abs(other_value - value) < 10 ** -decimals:
                score += 2
                break

    # A name that continues, or is continued by, an adjacent row: wrapped
    # description text that was split into its own row.
    for neighbour in (index - 1, index + 1):
        if 0 <= neighbour < len(items):
            other = _norm_name(items[neighbour])
            if name and other and (name in other or other in name):
                score += 2
                break

    # The existing tax/fee/discount vocabulary, kept as a weaker tie-breaker.
    if _is_likely_non_contributing(item.get("translated_name") or item.get("name")):
        score += 1

    # Rows contributing nothing are safe to shed when the arithmetic asks.
    if value == 0:
        score += 1
    return score


def find_surplus_removal(
    items: list[dict], receipt_total: float, decimals: int, tolerance: float
) -> Optional[list[dict]]:
    """Smallest, most-suspicious set of rows whose removal makes the remaining
    rows sum to receipt_total exactly. None when no bounded set does.

    Ties are broken toward the more suspicious rows and, failing that, toward
    later rows: a wrapped continuation always follows the row it belongs to.
    """
    if not items or len(items) > MAX_DROP_CANDIDATES:
        return None
    if _items_sum(items, decimals) - receipt_total <= tolerance:
        return None  # not an overshoot; nothing here to repair

    order = sorted(
        range(len(items)),
        key=lambda i: (-_suspicion(items, i, decimals), -i),
    )
    for size in range(1, min(MAX_DROPPED_ROWS, len(items) - 1) + 1):
        best = None
        for combo in combinations(order, size):
            drop = set(combo)
            kept = [it for i, it in enumerate(items) if i not in drop]
            if not kept:
                continue
            if abs(_items_sum(kept, decimals) - receipt_total) < tolerance:
                rank = (
                    -sum(_suspicion(items, i, decimals) for i in combo),
                    -max(combo),
                )
                if best is None or rank < best[0]:
                    best = (rank, kept)
        if best is not None:
            return best[1]
    return None


def verify_and_repair(annotation: dict, markdown: str = "") -> dict:
    """Repair surplus line-item rows in place. Returns metrics.

    A no-op unless the receipt has a total, the rows overshoot it, and a bounded
    set of rows accounts for the overshoot exactly. `markdown` is accepted for
    interface stability with other verification stages; this repair needs only
    the arithmetic.
    """
    metrics = {
        "verify_reconciled_before": None,
        "verify_reconciled_after": None,
        "verify_rows_dropped": 0,
    }
    if not annotation:
        return metrics

    items = annotation.get("items") or []
    receipt_total = _to_float(annotation.get("receipt_total"))
    if receipt_total is None or not items:
        return metrics

    decimals = _annotation_decimals(annotation)
    tolerance = _annotation_tolerance(annotation)
    total = bill_total(annotation)
    if total is None:
        total = _items_sum(items, decimals)
    before = abs(total - receipt_total) < tolerance
    metrics["verify_reconciled_before"] = before
    metrics["verify_reconciled_after"] = before
    if before:
        return metrics

    # Only item rows are candidates for removal: a duplicated row is a
    # transcription artefact, whereas an adjustment was already proved additive
    # by the arithmetic that put it there. So the rows have to hit whatever the
    # receipt total leaves over once the adjustments are accounted for.
    charged = round(total - _items_sum(items, decimals), decimals)
    kept = find_surplus_removal(
        items, round(receipt_total - charged, decimals), decimals, tolerance
    )
    if kept is None:
        return metrics

    metrics["verify_rows_dropped"] = len(items) - len(kept)
    annotation["items"] = kept
    recompute_bill_totals(annotation, decimals)
    annotation["totals_reconciled"] = True
    metrics["verify_reconciled_after"] = True
    logger.info(
        "Verify stage dropped %d surplus row(s) to reconcile items to receipt_total",
        metrics["verify_rows_dropped"],
    )
    return metrics
