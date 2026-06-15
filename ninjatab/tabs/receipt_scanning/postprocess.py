"""Pure, framework-light post-processing utilities for receipt annotations.

Each function here is a single reusable unit of work operating on a plain
annotation dict (the parsed Mistral `document_annotation`). Strategies compose
these into a post-processing stage; `standard_post_process` is the default
composition that mirrors current production behaviour.
"""

import difflib
import logging
import re
from collections import Counter
from itertools import combinations
from typing import Optional

from ninjatab.currencies.currency_utils import CURRENCY_DECIMAL_PLACES, get_decimal_places
from ninjatab.currencies.models import Currency

SUPPORTED_CURRENCY_CODES = frozenset(c.value for c in Currency)

logger = logging.getLogger("app")


NON_CONTRIBUTING_KEYWORDS = (
    "tax", "vat", "gst", "hst", "pst",
    "fee", "fees", "charge", "charges", "surcharge",
    "tip", "tips", "gratuity",
    "subtotal", "sub total", "sub-total",
    "discount", "discounts", "voucher", "loyalty", "promo", "promotion",
    "offer", "saving", "savings", "clubcard", "multibuy", "bogof",
    "rounding",
)

_NON_CONTRIBUTING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in NON_CONTRIBUTING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _annotation_decimals(annotation: dict) -> int:
    code = (annotation.get("currency_code") or "").strip().upper()
    if code and code not in CURRENCY_DECIMAL_PLACES:
        logger.warning(
            "Unknown currency_code %r in receipt annotation; defaulting to 2 decimal places",
            code,
        )
    return get_decimal_places(code)


def _annotation_tolerance(annotation: dict) -> float:
    # One minor unit of the receipt's currency (e.g. 0.01 USD, 0.001 JOD, 1 JPY).
    dp = _annotation_decimals(annotation)
    return 10 ** -dp if dp > 0 else 1.0


def _is_likely_non_contributing(name: Optional[str]) -> bool:
    return bool(name) and _NON_CONTRIBUTING_RE.search(name) is not None


def _normalize_amount_str(value, currency_decimals: int = 2):
    """Normalize an amount string to use '.' as the decimal separator and no
    thousands separators. Handles both '.'-decimal (US: 1,234.56) and
    ','-decimal (EU: 1.234,56) conventions, plus mixed/ambiguous cases.

    Rule: the rightmost of '.' or ',' is the decimal separator; the other is
    a thousands separator and is stripped. A lone separator followed by
    exactly 3 digits with no other separator is treated as a thousands
    separator (e.g. '1,234' or '1.500' → '1234') - except when the currency
    uses 3 decimal places (JOD, KWD, etc.), where '1.500' is 1.5 JOD."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value

    last_comma = s.rfind(",")
    last_dot = s.rfind(".")
    if last_comma == -1 and last_dot == -1:
        return s

    if last_comma > last_dot:
        decimal_sep, thousands_sep, decimal_pos = ",", ".", last_comma
    else:
        decimal_sep, thousands_sep, decimal_pos = ".", ",", last_dot

    frac = s[decimal_pos + 1:]
    # Lone separator + 3-digit "fraction" + no other separator → thousands,
    # unless the currency itself uses 3 decimal places (then it's the fraction).
    if (
        currency_decimals != 3
        and len(frac) == 3
        and frac.isdigit()
        and thousands_sep not in s
        and s.count(decimal_sep) == 1
    ):
        return s.replace(decimal_sep, "")

    integer = s[:decimal_pos].replace(thousands_sep, "").replace(decimal_sep, "")
    return f"{integer}.{frac}"


def _normalize_amounts_in_annotation(annotation: dict) -> None:
    """In-place: rewrite all amount strings on the annotation to use '.' as
    decimal separator so the mobile client parses them correctly."""
    dp = get_decimal_places((annotation.get("currency_code") or "").strip().upper())
    for key in ("receipt_total", "items_total"):
        if key in annotation:
            annotation[key] = _normalize_amount_str(annotation[key], dp)
    for item in annotation.get("items") or []:
        for key in ("total", "pre_discount_line_total", "post_discount_line_total", "price_per_quantity"):
            if key in item:
                item[key] = _normalize_amount_str(item[key], dp)
        discount = item.get("discount")
        if isinstance(discount, list):
            item["discount"] = [_normalize_amount_str(d, dp) for d in discount]
        elif discount is not None:
            item["discount"] = _normalize_amount_str(discount, dp)
    for adjustment in annotation.get("adjustments") or []:
        if "amount" in adjustment:
            adjustment["amount"] = _normalize_amount_str(adjustment["amount"], dp)


def _synthesize_total_only_item(annotation: dict) -> bool:
    """Card-terminal slips, ATM receipts, parking ticket stubs etc. often show
    only a grand total. The model correctly returns no items but a receipt_total.
    Synthesize a single item from the total so the bill is usable; otherwise
    the user sees an empty list with a total they can't split.

    Returns True if an item was synthesized. Mutates annotation in place."""
    items = annotation.get("items") or []
    if items:
        return False
    receipt_total = _to_float(annotation.get("receipt_total"))
    if receipt_total is None or receipt_total <= 0:
        return False
    dp = _annotation_decimals(annotation)
    name = (annotation.get("receipt_establishment_name") or "").strip() or "Total"
    annotation["items"] = [{
        "name": name,
        "translated_name": name,
        "total": f"{receipt_total:.{dp}f}",
    }]
    return True


def _collapse_redundant_translations(annotation: dict) -> None:
    """If a translated_name equals the original name (case-insensitive), drop
    the translation and reuse the original to preserve its casing."""
    for item in annotation.get("items") or []:
        name = item.get("name")
        translated = item.get("translated_name")
        if name and translated and name.casefold() == translated.casefold():
            item["translated_name"] = name
    for adjustment in annotation.get("adjustments") or []:
        name = adjustment.get("name")
        translated = adjustment.get("translated_name")
        if name and translated and name.casefold() == translated.casefold():
            adjustment["translated_name"] = name


def _attribute_item_adjustments(annotation: dict) -> int:
    """Fold item-flagged adjustments back onto the items they belong to.

    The model now records every saving in `adjustments`, flagging item-specific
    ones with relates_to_item=true and related_item_index pointing at the item.
    Here we move each such saving into that item's working `discount` list (so the
    downstream item-discount machinery is unchanged) and drop it from adjustments.

    An adjustment that relates to an item but cannot be resolved (missing/out-of-
    range index) is left in adjustments as a basket-level entry so it still affects
    reconciliation. The relates_to_item / related_item_index helper keys are
    stripped from every surviving adjustment so the saved annotation keeps its
    prior shape. Returns the count of attributed savings. Mutates in place."""
    items = annotation.get("items") or []
    adjustments = annotation.get("adjustments") or []
    survivors: list[dict] = []
    attributed = 0
    for adjustment in adjustments:
        relates = adjustment.get("relates_to_item")
        index = adjustment.get("related_item_index")
        amount = _to_float(adjustment.get("amount"))
        if relates and isinstance(index, int) and 0 <= index < len(items) and amount:
            item = items[index]
            discount = item.get("discount")
            item["discount"] = (discount if isinstance(discount, list) else []) + [adjustment["amount"]]
            attributed += 1
            continue
        adjustment.pop("relates_to_item", None)
        adjustment.pop("related_item_index", None)
        survivors.append(adjustment)
    annotation["adjustments"] = survivors or None
    return attributed


def _coerce_item_discounts(value) -> list[float]:
    """Coerce an item's ``discount`` into a list of non-zero floats. The model is
    asked for a list of negative strings (one per printed saving), but tolerate a
    bare string/number too since the raw OCR JSON is not schema-validated."""
    raw = value if isinstance(value, list) else [value]
    out = []
    for entry in raw:
        amount = _to_float(entry)
        if amount is not None and amount != 0:
            out.append(amount)
    return out


def _apply_item_discounts(annotation: dict) -> int:
    """Resolve each item's canonical net `total` from the model's pre/post line
    totals and discounts, so downstream (splitting, scoring, items_total) sees a
    single charged amount per item.

    The model transcribes a `pre_discount_line_total` (regular printed price) and,
    when a discounted/charged price is printed, a `post_discount_line_total`
    (e.g. a Clubcard/CC price), plus any printed savings in `discount`. We pick
    the net charged amount per the items_total rule:
      - post_discount_line_total when present;
      - else pre_discount_line_total when there is no item-level discount;
      - else (discount present, no printed charged price) pre - sum(|discount|),
        the only case where the server does the arithmetic.
    `total` is set to the net charged amount, the normalized `pre_discount_line_total`
    and `post_discount_line_total` (= net) are kept in the output so the full
    breakdown is visible, and `discount` holds the list of individual savings
    (synthesized from the pre/post gap when the model gave no explicit saving).

    Discounts that meet or exceed the line total are almost certainly a misparse,
    so they are dropped and the pre-discount price kept. An item that already
    carries a `total` (e.g. a synthesized total-only item) and no pre/post is left
    untouched. Returns the number of items that ended up with a discount. Mutates
    annotation in place."""
    dp = _annotation_decimals(annotation)
    discounted = 0
    for item in annotation.get("items") or []:
        discounts = _coerce_item_discounts(item.get("discount"))
        pre = _to_float(item.get("pre_discount_line_total"))
        post = _to_float(item.get("post_discount_line_total"))
        saving = sum(abs(d) for d in discounts)

        if post is not None:
            net = post
        elif pre is not None and not discounts:
            net = pre
        elif pre is not None:
            net = round(pre - saving, dp)
        else:
            # No pre/post from the model: keep any pre-existing `total` as-is.
            net = _to_float(item.get("total"))

        # Drop savings that don't reconcile with the printed prices (misparse).
        if discounts and net is not None and net < 0:
            logger.warning(
                "Item discount(s) %s exceed line total (pre=%s post=%s) for %r; dropping discount",
                discounts, pre, post, item.get("name"),
            )
            discounts = []
            net = pre if pre is not None else _to_float(item.get("total"))

        if net is not None:
            item["total"] = f"{net:.{dp}f}"

        # When the model gives a charged price below the regular price but no
        # explicit saving line, surface the implied saving so it is itemised and
        # visible (e.g. a transcribed Clubcard/CC price with no separate "-x.xx").
        if not discounts and pre is not None and net is not None and round(pre - net, dp) >= 10 ** -dp:
            discounts = [-round(pre - net, dp)]

        # Keep the resolved pre/post line totals in the saved annotation so the
        # full discount breakdown is visible. post mirrors the net charged amount
        # (`total`); `original_total` would duplicate pre, so it is dropped.
        if pre is not None:
            item["pre_discount_line_total"] = f"{pre:.{dp}f}"
        else:
            item.pop("pre_discount_line_total", None)
        if net is not None:
            item["post_discount_line_total"] = f"{net:.{dp}f}"
        else:
            item.pop("post_discount_line_total", None)
        item.pop("original_total", None)

        if discounts:
            item["discount"] = [f"{-abs(d):.{dp}f}" for d in discounts]
            discounted += 1
        else:
            item.pop("discount", None)
    return discounted


def _to_float(value) -> Optional[float]:
    """Coerce a Mistral-returned amount (now typed as string) into a float.
    Returns None on missing or unparseable input."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _item_charged_total(item: dict) -> Optional[float]:
    """The amount an item contributes to items_total: the post-discount (charged)
    line total when available, falling back to the resolved net `total`, then to
    the pre-discount line total."""
    for key in ("post_discount_line_total", "total", "pre_discount_line_total"):
        value = _to_float(item.get(key))
        if value is not None:
            return value
    return None


def _items_sum(items: list[dict], decimals: int = 2) -> float:
    return round(sum(_item_charged_total(i) or 0 for i in items), decimals)


def _reconcile_items_with_total(annotation: dict) -> list[dict]:
    """Try to make items_total match receipt_total by adjusting which
    tax/tip/fee-like rows are counted as items.

    - If items_total > receipt_total: try removing suspicious items already in
      the items list (matched on translated_name).
    - If items_total < receipt_total: try adding the receipt-level adjustments
      back into items.

    Returns the (possibly unchanged) items list."""
    items: list[dict] = list(annotation.get("items") or [])
    receipt_total = _to_float(annotation.get("receipt_total"))
    if receipt_total is None:
        return items
    if not items and not _candidate_additions(annotation):
        return items

    dp = _annotation_decimals(annotation)
    tolerance = _annotation_tolerance(annotation)
    diff = _items_sum(items, dp) - receipt_total

    if abs(diff) < tolerance:
        return items

    if diff > 0:
        suspicious = [
            i for i, it in enumerate(items)
            if _is_likely_non_contributing(it.get("translated_name"))
        ]
        for r in range(1, len(suspicious) + 1):
            for combo in combinations(suspicious, r):
                drop = set(combo)
                kept = [it for i, it in enumerate(items) if i not in drop]
                if abs(_items_sum(kept, dp) - receipt_total) < tolerance:
                    return kept
        return items

    additions = _candidate_additions(annotation)
    if not additions:
        return items
    idxs = list(range(len(additions)))
    for r in range(1, len(additions) + 1):
        for combo in combinations(idxs, r):
            extra = [additions[i] for i in combo]
            if abs(_items_sum(items + extra, dp) - receipt_total) < tolerance:
                return items + extra
    return items


_ADJUSTMENT_KIND_LABELS = {
    "tax": "Tax",
    "tip": "Tip",
    "discount": "Discount",
    "fee": "Fee",
    "other": "Charge",
}


def _candidate_additions(annotation: dict) -> list[dict]:
    """Build a list of item-shaped dicts from the receipt-level adjustments, used
    when items_total is below receipt_total and we suspect a contributing charge
    (tax/tip/fee) was not represented as a line item.

    Item totals are emitted as strings formatted to the receipt currency's
    precision, matching the type the model-returned items use. Adjustment amounts
    are signed, so a subtractive adjustment (discount) yields a negative-total
    candidate."""
    dp = _annotation_decimals(annotation)
    out: list[dict] = []
    for adjustment in annotation.get("adjustments") or []:
        amount = _to_float(adjustment.get("amount"))
        if amount is None:
            continue
        label = adjustment.get("name") or _ADJUSTMENT_KIND_LABELS.get(adjustment.get("kind"), "Charge")
        out.append({
            "name": label,
            "translated_name": adjustment.get("translated_name") or label,
            "total": f"{amount:.{dp}f}",
        })
    return out


def standard_post_process(annotation: dict, default_currency: str) -> dict:
    """Apply the standard production post-processing stage to a single parsed
    annotation, in place, and return a dict of metrics describing what happened.

    Composes: currency fallback → amount normalization → total-only synthesis →
    item/receipt-total reconciliation → items_total computation → translation
    collapse → totals metrics. This is the one stage a strategy may override or
    duplicate wholesale; the individual steps above remain reusable on their own.
    """
    metrics: dict = {
        "annotation_present": True,
        "currency_source": None,            # "model" | "fallback_missing" | "fallback_unsupported"
        "currency_code": None,
        "currency_decimals": None,
        "items_count": 0,
        "items_total": None,
        "ai_items_total": None,
        "receipt_total": None,
        "receipt_total_visible": None,      # model's report of whether a grand total is legibly printed
        "items_match_receipt_total": None,  # None when receipt_total absent
        "items_receipt_gap": None,
        "ai_vs_server_total_divergence": None,
        "has_tax": False,
        "has_tip": False,                   # tip / gratuity / service charge (one kind)
        "adjustments_count": 0,
        "item_adjustments_attributed": 0,   # count of item-flagged adjustments folded back onto items
        "item_discounts_applied": 0,        # count of items with an item-level discount folded in
        "reconciliation_action": "none",    # "none" | "items_dropped" | "candidates_added"
        "reconciliation_items_delta": 0,
        "synthesized_total_only_item": False,
    }

    raw_code = (annotation.get("currency_code") or "").strip().upper()
    if not raw_code:
        logger.warning(
            "Mistral OCR returned no currency_code; falling back to default %s",
            default_currency,
        )
        annotation["currency_code"] = default_currency
        metrics["currency_source"] = "fallback_missing"
    elif raw_code not in SUPPORTED_CURRENCY_CODES:
        logger.warning(
            "Mistral OCR returned unsupported currency_code %r; falling back to default %s",
            raw_code, default_currency,
        )
        annotation["currency_code"] = default_currency
        metrics["currency_source"] = "fallback_unsupported"
        metrics["currency_unsupported_raw"] = raw_code
    else:
        annotation["currency_code"] = raw_code
        metrics["currency_source"] = "model"
    metrics["currency_code"] = annotation["currency_code"]
    metrics["currency_decimals"] = _annotation_decimals(annotation)

    _normalize_amounts_in_annotation(annotation)
    metrics["item_adjustments_attributed"] = _attribute_item_adjustments(annotation)
    metrics["item_discounts_applied"] = _apply_item_discounts(annotation)
    annotation["ai_items_total"] = annotation.pop("items_total", None)
    metrics["synthesized_total_only_item"] = _synthesize_total_only_item(annotation)
    items_before = list(annotation.get("items") or [])
    annotation["items"] = _reconcile_items_with_total(annotation)
    items_after = annotation["items"]
    delta = len(items_after) - len(items_before)
    if delta > 0:
        metrics["reconciliation_action"] = "candidates_added"
    elif delta < 0:
        metrics["reconciliation_action"] = "items_dropped"
    metrics["reconciliation_items_delta"] = delta

    annotation["items_total"] = _items_sum(items_after, _annotation_decimals(annotation))
    _collapse_redundant_translations(annotation)

    receipt_total_f = _to_float(annotation.get("receipt_total"))
    items_total_f = _to_float(annotation.get("items_total"))
    ai_items_total_f = _to_float(annotation.get("ai_items_total"))
    tolerance = _annotation_tolerance(annotation)
    metrics["items_count"] = len(items_after)
    metrics["items_total"] = items_total_f
    metrics["ai_items_total"] = ai_items_total_f
    metrics["receipt_total"] = receipt_total_f
    metrics["receipt_total_visible"] = annotation.get("receipt_total_visible")
    if receipt_total_f is not None and items_total_f is not None:
        gap = round(items_total_f - receipt_total_f, 6)
        metrics["items_receipt_gap"] = gap
        metrics["items_match_receipt_total"] = abs(gap) < tolerance
        # Surface the reconciliation outcome on the annotation itself so the
        # client can flag low-confidence parses (items_total != receipt_total)
        # for user review.
        annotation["totals_reconciled"] = metrics["items_match_receipt_total"]
    if items_total_f is not None and ai_items_total_f is not None:
        metrics["ai_vs_server_total_divergence"] = (
            abs(items_total_f - ai_items_total_f) > tolerance
        )
    adjustments = annotation.get("adjustments") or []
    metrics["adjustments_count"] = len(adjustments)
    metrics["has_tax"] = any(a.get("kind") == "tax" for a in adjustments)
    metrics["has_tip"] = any(a.get("kind") == "tip" for a in adjustments)

    return metrics


def _items_receipt_gap(annotation: dict) -> Optional[float]:
    """Absolute gap between the server-calculated items_total and receipt_total
    for an already post-processed annotation. None when either is missing."""
    items_total = _to_float(annotation.get("items_total"))
    receipt_total = _to_float(annotation.get("receipt_total"))
    if items_total is None or receipt_total is None:
        return None
    return abs(items_total - receipt_total)


def _is_reconciled(annotation: dict) -> bool:
    """True when the calculated items_total matches receipt_total within the
    currency's tolerance. False when they diverge or receipt_total is absent."""
    gap = _items_receipt_gap(annotation)
    return gap is not None and gap < _annotation_tolerance(annotation)


def recompute_total_match(annotation: dict) -> dict:
    """Recompute items_total and the reconciliation flag in place (used after
    swapping in consensus field values). Returns the totals metrics."""
    dp = _annotation_decimals(annotation)
    annotation["items_total"] = _items_sum(annotation.get("items") or [], dp)
    items_total = _to_float(annotation.get("items_total"))
    receipt_total = _to_float(annotation.get("receipt_total"))
    tolerance = _annotation_tolerance(annotation)
    gap = round(items_total - receipt_total, 6) if (items_total is not None and receipt_total is not None) else None
    matched = abs(gap) < tolerance if gap is not None else None
    if receipt_total is not None:
        annotation["totals_reconciled"] = matched
    return {
        "items_total": items_total,
        "receipt_total": receipt_total,
        "items_receipt_gap": gap,
        "items_match_receipt_total": matched,
    }


def select_best_line_items(candidates: list[dict]) -> Optional[int]:
    """Pick the candidate with the best line-item breakdown, using the
    items_total-vs-receipt_total gap as the dominant correctness signal but
    refusing to reward merged/dropped rows.

    Ranking (highest first):
      1. reconciled (items_total matches receipt_total within tolerance)
      2. item count equals the modal count among the reconciled candidates
         (a candidate that merged rows to hit the total has fewer items than
         the mode and loses here)
      3. has a currency_code
      4. has a datetime_of_receipt
      5. smallest items/receipt gap

    Returns None when no candidate is valid."""
    valid_idx = [i for i, c in enumerate(candidates) if c]
    if not valid_idx:
        return None

    reconciled_idx = [i for i in valid_idx if _is_reconciled(candidates[i])]
    pool = reconciled_idx or valid_idx
    counts = Counter(len(candidates[i].get("items") or []) for i in pool)
    modal_count = counts.most_common(1)[0][0] if counts else None

    def score(i: int):
        c = candidates[i]
        gap = _items_receipt_gap(c)
        return (
            1 if i in reconciled_idx else 0,
            1 if modal_count is not None and len(c.get("items") or []) == modal_count else 0,
            1 if c.get("currency_code") else 0,
            1 if c.get("datetime_of_receipt") else 0,
            -(gap if gap is not None else float("inf")),
        )

    return max(valid_idx, key=score)


def field_consensus(candidates: list[dict]) -> dict:
    """Modal value across candidates for the stable scalar fields. These vote
    cleanly (currency flips, establishment null-outs, missing dates are
    outliers), so the mode removes per-run noise. Line items are NOT voted here
    - they are taken whole from the best candidate (see select_best_line_items)
    to keep the itemization internally coherent."""
    valid = [c for c in candidates if c]

    def mode(key):
        counts = Counter(c.get(key) for c in valid if c.get(key) is not None)
        return counts.most_common(1)[0][0] if counts else None

    return {
        k: mode(k)
        for k in ("currency_code", "receipt_establishment_name", "datetime_of_receipt", "receipt_total")
    }


def most_common_consensus(candidates: list[dict]) -> Optional[int]:
    """Fallback selection when no candidate has a receipt_total to anchor on.
    Picks the candidate that best agrees with the field-wise modal values across
    candidates (establishment name, currency, item count). Returns the index of
    the first valid candidate if there is no signal."""
    valid = [(i, c) for i, c in enumerate(candidates) if c]
    if not valid:
        return None

    def mode(values):
        counts = Counter(v for v in values if v is not None)
        return counts.most_common(1)[0][0] if counts else None

    modal_name = mode([c.get("receipt_establishment_name") for _, c in valid])
    modal_currency = mode([c.get("currency_code") for _, c in valid])
    modal_count = mode([len(c.get("items") or []) for _, c in valid])

    best_idx, best_score = valid[0][0], -1
    for idx, c in valid:
        score = 0
        if modal_name is not None and c.get("receipt_establishment_name") == modal_name:
            score += 1
        if modal_currency is not None and c.get("currency_code") == modal_currency:
            score += 1
        if modal_count is not None and len(c.get("items") or []) == modal_count:
            score += 1
        if score > best_score:
            best_score, best_idx = score, idx
    return best_idx


# --- reconciliation-weighted, cell-level consensus -------------------------
#
# The selection-based consensus above takes one candidate's items wholesale.
# The voting consensus below instead keeps the best candidate only as a row
# skeleton and votes each cell across all candidates, weighting every candidate
# by how well its items reconcile to its receipt_total. Text fields use a
# weighted mode, numeric fields a weighted median, so a single bad pass cannot
# swing a field it is outvoted on.

def candidate_weight(annotation: Optional[dict]) -> float:
    """Trust weight for a candidate: 1.0 when its items reconcile to
    receipt_total, decaying with the relative gap (floored so an unreconciled
    candidate still contributes a little), 0.5 when there is no receipt_total to
    check against, 0.0 for a missing candidate."""
    if not annotation:
        return 0.0
    gap = _items_receipt_gap(annotation)
    if gap is None:
        return 0.5
    if gap < _annotation_tolerance(annotation):
        return 1.0
    receipt_total = _to_float(annotation.get("receipt_total")) or 0.0
    rel = gap / receipt_total if receipt_total else 1.0
    return max(0.25, 1.0 - rel)


def _weighted_mode(values_weights) -> Optional[object]:
    """Value with the highest summed weight (ties broken by first appearance).
    None values are ignored."""
    totals: dict = {}
    order: list = []
    for value, weight in values_weights:
        if value is None:
            continue
        if value not in totals:
            totals[value] = 0.0
            order.append(value)
        totals[value] += weight
    if not order:
        return None
    return max(order, key=lambda v: totals[v])


def _weighted_median(values_weights) -> Optional[float]:
    """Weighted median of numeric (value, weight) pairs. None if no values."""
    pairs = sorted((v, w) for v, w in values_weights if v is not None and w > 0)
    if not pairs:
        return None
    half = sum(w for _, w in pairs) / 2.0
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= half:
            return value
    return pairs[-1][0]


def _align_to_anchor(anchor_items: list[dict], other_items: list[dict]) -> list[Optional[dict]]:
    """Greedy one-to-one alignment of ``other_items`` onto ``anchor_items`` by
    name similarity. Returns a list parallel to ``anchor_items`` holding the
    matched other-item (or None when nothing matches above a minimal similarity,
    so unrelated rows are not forced together)."""
    remaining = list(range(len(other_items)))
    matched: list[Optional[dict]] = []
    for anchor_item in anchor_items:
        anchor_name = (anchor_item.get("name") or "").lower()
        best_j, best_score = None, -1.0
        for j in remaining:
            other_name = (other_items[j].get("name") or "").lower()
            score = difflib.SequenceMatcher(None, anchor_name, other_name).ratio()
            if score > best_score:
                best_score, best_j = score, j
        if best_j is not None and best_score >= 0.5:
            remaining.remove(best_j)
            matched.append(other_items[best_j])
        else:
            matched.append(None)
    return matched


def _vote_item(anchor_item: dict, cells: list[tuple[dict, float]], decimals: int) -> dict:
    """Vote one merged item from aligned (item, weight) cells. Starts from the
    anchor row, then overlays voted core fields: text by weighted mode, numerics
    by weighted median, formatted to the receipt currency's precision."""
    item = dict(anchor_item)
    for key in ("name", "translated_name"):
        voted = _weighted_mode((it.get(key), w) for it, w in cells)
        if voted is not None:
            item[key] = voted
    for key in ("total", "pre_discount_line_total", "price_per_quantity"):
        voted = _weighted_median((_to_float(it.get(key)), w) for it, w in cells)
        if voted is not None:
            item[key] = f"{voted:.{decimals}f}"
    quantity = _weighted_median((_to_float(it.get("quantity")), w) for it, w in cells)
    if quantity is not None:
        item["quantity"] = int(quantity) if float(quantity).is_integer() else quantity

    # Keep the discount breakdown consistent with the voted prices: the charged
    # (post-discount) total mirrors the voted net `total`, and the saving is
    # re-derived from the voted pre/post gap.
    total = _to_float(item.get("total"))
    pre = _to_float(item.get("pre_discount_line_total"))
    if total is not None:
        item["post_discount_line_total"] = f"{total:.{decimals}f}"
        if pre is not None and round(pre - total, decimals) >= 10 ** -decimals:
            item["discount"] = [f"{-round(pre - total, decimals):.{decimals}f}"]
        else:
            item.pop("discount", None)
    return item


def merge_candidates_by_voting(candidates: list[dict]) -> tuple[Optional[int], Optional[dict]]:
    """Reconciliation-weighted, cell-level consensus across candidates.

    Uses the best-structured candidate (``select_best_line_items``) as the row
    skeleton, aligns every candidate's items onto it, then votes each item field
    and each scalar field across the aligned candidates — each candidate weighted
    by ``candidate_weight``. Returns (anchor_index, merged_annotation), or
    (None, None) when no candidate is usable."""
    anchor_idx = select_best_line_items(candidates)
    if anchor_idx is None:
        return None, None

    anchor = candidates[anchor_idx]
    weighted = [(c, candidate_weight(c)) for c in candidates if c]
    decimals = _annotation_decimals(anchor)
    anchor_items = anchor.get("items") or []

    aligned = [(_align_to_anchor(anchor_items, c.get("items") or []), w) for c, w in weighted]

    merged_items = []
    for row, anchor_item in enumerate(anchor_items):
        cells = [(matched[row], w) for matched, w in aligned if matched[row] is not None]
        if not cells:
            cells = [(anchor_item, candidate_weight(anchor) or 1.0)]
        merged_items.append(_vote_item(anchor_item, cells, decimals))

    merged = dict(anchor)
    merged["items"] = merged_items

    receipt_total = _weighted_median((_to_float(c.get("receipt_total")), w) for c, w in weighted)
    if receipt_total is not None:
        merged["receipt_total"] = f"{receipt_total:.{decimals}f}"
    for key in ("currency_code", "receipt_establishment_name", "datetime_of_receipt"):
        voted = _weighted_mode((c.get(key), w) for c, w in weighted)
        if voted is not None:
            merged[key] = voted

    return anchor_idx, merged
