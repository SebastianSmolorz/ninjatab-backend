"""Pure, framework-light post-processing utilities for receipt annotations.

Each function here is a single reusable unit of work operating on a plain
annotation dict (the parsed Mistral `document_annotation`). Strategies compose
these into a post-processing stage; `standard_post_process` is the default
composition that mirrors current production behaviour.

The v2 annotation is a ledger: items (with adjustments folded to net totals)
plus receipt-level `charges`, each flagged `included_in_item_totals`. Two
identities anchor reconciliation:
  I1: subtotal ≈ sum(item totals)
  I2: receipt_total ≈ sum(item totals) + sum(non-included charge amounts)
"""

import copy
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
    "rounding",
)

_NON_CONTRIBUTING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in NON_CONTRIBUTING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_TIP_RE = re.compile(r"\b(?:tips?|gratuity)\b", re.IGNORECASE)


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


# Narrower groups (subset of NON_CONTRIBUTING_KEYWORDS) used to tag items the
# client should default to a proportional split. Subtotal/discount/rounding are
# deliberately excluded — they are not charges to redistribute.
_CATEGORY_RES = (
    ("tax", re.compile(r"\b(?:tax|vat|gst|hst|pst)\b", re.IGNORECASE)),
    ("tip", _TIP_RE),
    ("service", re.compile(r"\b(?:service\s*charge|surcharge|service\s*fee)\b", re.IGNORECASE)),
)


def _categorize_item_name(name: Optional[str]) -> str:
    """Classify a line-item name as 'tax' | 'tip' | 'service' | 'item'. Used to
    tell the client which scanned rows should default to a proportional split."""
    if name:
        for category, regex in _CATEGORY_RES:
            if regex.search(name):
                return category
    return "item"


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
    for key in ("receipt_total", "items_total", "subtotal"):
        if key in annotation:
            annotation[key] = _normalize_amount_str(annotation[key], dp)
    for item in annotation.get("items") or []:
        for key in ("total", "price_per_quantity"):
            if key in item:
                item[key] = _normalize_amount_str(item[key], dp)
        for adjustment in item.get("adjustments") or []:
            if "amount" in adjustment:
                adjustment["amount"] = _normalize_amount_str(adjustment["amount"], dp)
    for charge in annotation.get("charges") or []:
        if "amount" in charge:
            charge["amount"] = _normalize_amount_str(charge["amount"], dp)


def _drop_useless_charges(annotation: dict) -> None:
    """In-place: remove charges with a zero or unparseable amount (e.g. a
    'Rounding 0.00' row) — pure noise for splitting and for old clients."""
    charges = annotation.get("charges")
    if charges:
        annotation["charges"] = [c for c in charges if _to_float(c.get("amount"))]


def _fold_item_adjustments(annotation: dict) -> int:
    """In-place: fold each item's adjustments into a net `total`. A replacement
    price (is_new_price) wins outright (last one if several); otherwise the net
    is the printed price plus the signed deltas. The printed price is kept on
    `gross_total` and the adjustments list is preserved for display/verification.

    Returns the number of items folded."""
    dp = _annotation_decimals(annotation)
    folded = 0
    for item in annotation.get("items") or []:
        adjustments = item.get("adjustments") or []
        gross = _to_float(item.get("total"))
        if not adjustments or gross is None:
            continue
        net = gross
        replacement = None
        for adj in adjustments:
            amount = _to_float(adj.get("amount"))
            if amount is None:
                continue
            if adj.get("is_new_price"):
                replacement = amount
            else:
                net += amount
        if replacement is not None:
            net = replacement
        tolerance = 10 ** -dp if dp > 0 else 1.0
        if abs(net - gross) < tolerance:
            continue
        item["gross_total"] = item["total"]
        item["total"] = f"{net:.{dp}f}"
        folded += 1
    return folded


def _unfold_item_adjustments(item: dict) -> bool:
    """Revert one item's fold (see _fold_item_adjustments). Returns True if the
    item had a fold to revert."""
    gross = item.get("gross_total")
    if gross is None:
        return False
    item["total"] = gross
    del item["gross_total"]
    return True


def _non_included_charges_sum(annotation: dict, decimals: int = 2) -> float:
    return round(
        sum(
            _to_float(c.get("amount")) or 0
            for c in annotation.get("charges") or []
            if not c.get("included_in_item_totals")
        ),
        decimals,
    )


def _synthesize_total_only_item(annotation: dict) -> bool:
    """Card-terminal slips, ATM receipts, parking ticket stubs etc. often show
    only a grand total. The model correctly returns no items but a receipt_total.
    Synthesize a single item from the total (minus any non-included charges, so
    they are not double counted) so the bill is usable; otherwise the user sees
    an empty list with a total they can't split.

    Returns True if an item was synthesized. Mutates annotation in place."""
    items = annotation.get("items") or []
    if items:
        return False
    receipt_total = _to_float(annotation.get("receipt_total"))
    if receipt_total is None or receipt_total <= 0:
        return False
    dp = _annotation_decimals(annotation)
    value = round(receipt_total - _non_included_charges_sum(annotation, dp), dp)
    if value <= 0:
        value = receipt_total
    name = (annotation.get("receipt_establishment_name") or "").strip() or "Total"
    annotation["items"] = [{
        "name": name,
        "translated_name": name,
        "total": f"{value:.{dp}f}",
    }]
    return True


def _collapse_redundant_translations(annotation: dict) -> None:
    """If a translated_name equals the original name (case-insensitive), drop
    the translation and reuse the original to preserve its casing."""
    def collapse(entry: dict) -> None:
        name = entry.get("name")
        translated = entry.get("translated_name")
        if name and translated and name.casefold() == translated.casefold():
            entry["translated_name"] = name

    for item in annotation.get("items") or []:
        collapse(item)
        for adjustment in item.get("adjustments") or []:
            collapse(adjustment)
    for charge in annotation.get("charges") or []:
        collapse(charge)


def _to_float(value) -> Optional[float]:
    """Coerce a Mistral-returned amount (typed as string) into a float.
    Returns None on missing or unparseable input."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _items_sum(items: list[dict], decimals: int = 2) -> float:
    return round(sum(_to_float(i.get("total")) or 0 for i in items), decimals)


def _reconcile_ledger(annotation: dict) -> str:
    """Try to make the ledger identity I2 hold by applying at most two repair
    moves, preferring fewest moves and then the smallest amount moved:

    - flip: toggle a charge's included_in_item_totals (the common case — the
      model misjudged whether a VAT line is already inside the item prices)
    - reclassify: move a tax/tip/fee-named item out of items into charges as an
      included charge (the model printed a charge row as an item)
    - unfold: revert one item's adjustment fold (the model had already reported
      the net total, so folding double-counted the adjustment)

    A repair is only accepted if it satisfies I2 without breaking an I1
    (subtotal == items sum) that held beforehand. Mutates the annotation in
    place when a repair is accepted. Returns the reconciliation action:
    'none' | 'flip_included' | 'reclassified_item' | 'unfolded_adjustments' | 'combo'.
    """
    receipt_total = _to_float(annotation.get("receipt_total"))
    if receipt_total is None:
        return "none"
    dp = _annotation_decimals(annotation)
    tolerance = _annotation_tolerance(annotation)
    subtotal = _to_float(annotation.get("subtotal"))

    def i2_gap(ann: dict) -> float:
        return _items_sum(ann.get("items") or [], dp) + _non_included_charges_sum(ann, dp) - receipt_total

    def i1_holds(ann: dict) -> Optional[bool]:
        if subtotal is None:
            return None
        return abs(_items_sum(ann.get("items") or [], dp) - subtotal) < tolerance

    if abs(i2_gap(annotation)) < tolerance:
        return "none"

    i1_held_before = i1_holds(annotation) is True

    # Build candidate moves as (action, magnitude, apply) over a working copy.
    def flip(idx):
        def apply(ann):
            charge = ann["charges"][idx]
            charge["included_in_item_totals"] = not charge.get("included_in_item_totals")
        return apply

    # Reclassify replaces the item with a None placeholder (compacted after all
    # moves apply) so item indices captured by other moves stay valid.
    def reclassify(idx):
        def apply(ann):
            item = ann["items"][idx]
            ann["items"][idx] = None
            name = item.get("name") or item.get("translated_name") or ""
            ann.setdefault("charges", []).append({
                "name": item.get("name") or "Charge",
                "translated_name": item.get("translated_name") or item.get("name") or "Charge",
                "kind": "tip" if _TIP_RE.search(name) else "charge",
                "amount": item.get("total"),
                "included_in_item_totals": True,
            })
        return apply

    def unfold(idx):
        def apply(ann):
            item = ann["items"][idx]
            if item:
                _unfold_item_adjustments(item)
        return apply

    moves = []
    for j, charge in enumerate(annotation.get("charges") or []):
        amount = _to_float(charge.get("amount"))
        if amount:
            moves.append(("flip_included", abs(amount), flip(j)))
    for i, item in enumerate(annotation.get("items") or []):
        total = _to_float(item.get("total"))
        if total and _is_likely_non_contributing(item.get("translated_name") or item.get("name")):
            moves.append(("reclassified_item", abs(total), reclassify(i)))
        if item.get("gross_total") is not None:
            delta = abs((_to_float(item.get("gross_total")) or 0) - (total or 0))
            if delta:
                moves.append(("unfolded_adjustments", delta, unfold(i)))

    # ponytail: bounded search — closest-to-gap moves first, singles then pairs,
    # at most 10 candidate moves. Enough for real receipts; anything wilder is
    # better left unreconciled for the user to review.
    gap = abs(i2_gap(annotation))
    moves.sort(key=lambda m: abs(m[1] - gap))
    moves = moves[:10]

    for r in (1, 2):
        for combo in combinations(range(len(moves)), r):
            candidate = copy.deepcopy(annotation)
            try:
                for k in combo:
                    moves[k][2](candidate)
            except (IndexError, KeyError, TypeError, AttributeError):
                continue
            candidate["items"] = [i for i in candidate.get("items") or [] if i is not None]
            if abs(i2_gap(candidate)) >= tolerance:
                continue
            if i1_held_before and i1_holds(candidate) is False:
                continue
            annotation.clear()
            annotation.update(candidate)
            actions = {moves[k][0] for k in combo}
            return actions.pop() if len(actions) == 1 and r == 1 else "combo"
    return "none"


def flatten_for_legacy(annotation: dict) -> dict:
    """Serialize a canonical v2 annotation into the legacy (v1) response shape
    old app builds expect: non-included charges folded into `items` with a
    category tag, legacy tax/tip scalars populated, v2-only keys dropped."""
    ann = copy.deepcopy(annotation)
    dp = _annotation_decimals(ann)
    charges = ann.pop("charges", None) or []
    ann.pop("subtotal", None)
    items = list(ann.get("items") or [])
    for item in items:
        item.pop("adjustments", None)
        item.pop("gross_total", None)

    tax_sum = 0.0
    tip_sum = 0.0
    for charge in charges:
        if charge.get("included_in_item_totals"):
            continue
        amount = _to_float(charge.get("amount"))
        if not amount:
            continue
        category = "tip" if charge.get("kind") == "tip" else "tax"
        if category == "tip":
            tip_sum += amount
        else:
            tax_sum += amount
        items.append({
            "name": charge.get("name") or "Charge",
            "translated_name": charge.get("translated_name") or charge.get("name") or "Charge",
            "total": f"{amount:.{dp}f}",
            "category": category,
        })

    ann["items"] = items
    ann["items_total"] = _items_sum(items, dp)
    ann["tax"] = f"{tax_sum:.{dp}f}" if tax_sum else None
    ann["tip"] = f"{tip_sum:.{dp}f}" if tip_sum else None
    return ann


def standard_post_process(annotation: dict, default_currency: str) -> dict:
    """Apply the standard production post-processing stage to a single parsed
    annotation, in place, and return a dict of metrics describing what happened.

    Composes: currency fallback → amount normalization → zero-charge pruning →
    adjustment folding → total-only synthesis → ledger reconciliation →
    items_total computation → translation collapse → totals metrics. This is the
    one stage a strategy may override or duplicate wholesale; the individual
    steps above remain reusable on their own.
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
        "items_match_receipt_total": None,  # None when receipt_total absent; now the ledger identity I2
        "items_receipt_gap": None,
        "ai_vs_server_total_divergence": None,
        "has_tax": False,
        "has_tip": False,
        "has_service_charge": False,        # retained for dashboard compat; always False in v2
        "other_charges_count": 0,
        "reconciliation_action": "none",
        "reconciliation_items_delta": 0,    # retained for dashboard compat
        "synthesized_total_only_item": False,
        "subtotal_present": False,
        "subtotal_matches_items": None,
        "charges_count": 0,
        "included_charges_count": 0,
        "item_adjustments_folded": 0,
        "ledger_gap": None,
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
    dp = _annotation_decimals(annotation)
    metrics["currency_decimals"] = dp

    _normalize_amounts_in_annotation(annotation)
    _drop_useless_charges(annotation)
    annotation["ai_items_total"] = annotation.pop("items_total", None)
    metrics["item_adjustments_folded"] = _fold_item_adjustments(annotation)
    metrics["synthesized_total_only_item"] = _synthesize_total_only_item(annotation)

    items_before = len(annotation.get("items") or [])
    metrics["reconciliation_action"] = _reconcile_ledger(annotation)
    metrics["reconciliation_items_delta"] = len(annotation.get("items") or []) - items_before

    items = annotation.get("items") or []
    annotation["items_total"] = _items_sum(items, dp)
    _collapse_redundant_translations(annotation)
    for item in items:
        item["category"] = _categorize_item_name(
            item.get("name") or item.get("translated_name")
        )

    charges = annotation.get("charges") or []
    receipt_total_f = _to_float(annotation.get("receipt_total"))
    items_total_f = _to_float(annotation.get("items_total"))
    ai_items_total_f = _to_float(annotation.get("ai_items_total"))
    subtotal_f = _to_float(annotation.get("subtotal"))
    tolerance = _annotation_tolerance(annotation)
    metrics["items_count"] = len(items)
    metrics["items_total"] = items_total_f
    metrics["ai_items_total"] = ai_items_total_f
    metrics["receipt_total"] = receipt_total_f
    metrics["subtotal_present"] = subtotal_f is not None
    if subtotal_f is not None and items_total_f is not None:
        metrics["subtotal_matches_items"] = abs(items_total_f - subtotal_f) < tolerance
    if receipt_total_f is not None and items_total_f is not None:
        gap = round(
            items_total_f + _non_included_charges_sum(annotation, dp) - receipt_total_f, 6
        )
        metrics["items_receipt_gap"] = gap
        metrics["ledger_gap"] = gap
        metrics["items_match_receipt_total"] = abs(gap) < tolerance
        # Surface the reconciliation outcome on the annotation itself so the
        # client can flag low-confidence parses for user review.
        annotation["totals_reconciled"] = metrics["items_match_receipt_total"]
    if items_total_f is not None and ai_items_total_f is not None:
        metrics["ai_vs_server_total_divergence"] = (
            abs(items_total_f - ai_items_total_f) > tolerance
        )
    metrics["has_tax"] = any(c.get("kind") == "charge" for c in charges)
    metrics["has_tip"] = any(c.get("kind") == "tip" for c in charges)
    metrics["charges_count"] = len(charges)
    metrics["included_charges_count"] = sum(
        1 for c in charges if c.get("included_in_item_totals")
    )
    metrics["other_charges_count"] = metrics["charges_count"]

    return metrics


def _items_receipt_gap(annotation: dict) -> Optional[float]:
    """Absolute ledger gap |items_total + Σ(non-included charges) − receipt_total|
    for an already post-processed annotation. None when either total is missing."""
    items_total = _to_float(annotation.get("items_total"))
    receipt_total = _to_float(annotation.get("receipt_total"))
    if items_total is None or receipt_total is None:
        return None
    dp = _annotation_decimals(annotation)
    return abs(items_total + _non_included_charges_sum(annotation, dp) - receipt_total)


def _is_reconciled(annotation: dict) -> bool:
    """True when the ledger identity holds within the currency's tolerance.
    False when it diverges or receipt_total is absent."""
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
    gap = (
        round(items_total + _non_included_charges_sum(annotation, dp) - receipt_total, 6)
        if (items_total is not None and receipt_total is not None)
        else None
    )
    matched = abs(gap) < tolerance if gap is not None else None
    if receipt_total is not None:
        annotation["totals_reconciled"] = matched
    return {
        "items_total": items_total,
        "receipt_total": receipt_total,
        "items_receipt_gap": gap,
        "ledger_gap": gap,
        "items_match_receipt_total": matched,
    }


def select_best_line_items(candidates: list[dict]) -> Optional[int]:
    """Pick the candidate with the best line-item breakdown, using the ledger
    gap as the dominant correctness signal but refusing to reward merged/dropped
    rows.

    Ranking (highest first):
      1. reconciled (ledger identity holds within tolerance)
      2. item count equals the modal count among the reconciled candidates
         (a candidate that merged rows to hit the total has fewer items than
         the mode and loses here)
      3. has a currency_code
      4. has a datetime_of_receipt
      5. smallest ledger gap

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
        for k in (
            "currency_code",
            "receipt_establishment_name",
            "datetime_of_receipt",
            "receipt_total",
            "subtotal",
        )
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
