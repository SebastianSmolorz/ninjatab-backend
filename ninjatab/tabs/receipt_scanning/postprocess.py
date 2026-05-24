"""Pure, framework-light post-processing utilities for receipt annotations.

Each function here is a single reusable unit of work operating on a plain
annotation dict (the parsed Mistral `document_annotation`). Strategies compose
these into a post-processing stage; `standard_post_process` is the default
composition that mirrors current production behaviour.
"""

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
    for key in ("receipt_total", "items_total", "tax", "tip", "service_charge"):
        if key in annotation:
            annotation[key] = _normalize_amount_str(annotation[key], dp)
    for item in annotation.get("items") or []:
        for key in ("total", "price_per_quantity"):
            if key in item:
                item[key] = _normalize_amount_str(item[key], dp)
    for charge in annotation.get("other_charges") or []:
        if "amount" in charge:
            charge["amount"] = _normalize_amount_str(charge["amount"], dp)


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
    for charge in annotation.get("other_charges") or []:
        name = charge.get("name")
        translated = charge.get("translated_name")
        if name and translated and name.casefold() == translated.casefold():
            charge["translated_name"] = name


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


def _items_sum(items: list[dict], decimals: int = 2) -> float:
    return round(sum(_to_float(i.get("total")) or 0 for i in items), decimals)


def _reconcile_items_with_total(annotation: dict) -> list[dict]:
    """Try to make items_total match receipt_total by adjusting which
    tax/tip/fee-like rows are counted as items.

    - If items_total > receipt_total: try removing suspicious items already in
      the items list (matched on translated_name).
    - If items_total < receipt_total: try adding from the dedicated
      tax/tip/service_charge/other_charges fields back into items.

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


def _candidate_additions(annotation: dict) -> list[dict]:
    """Build a list of item-shaped dicts from the dedicated charge fields,
    used when items_total is below receipt_total and we suspect a contributing
    charge was misrouted into tax/tip/service_charge/other_charges.

    Item totals are emitted as strings formatted to the receipt currency's
    precision, matching the type the model-returned items use."""
    dp = _annotation_decimals(annotation)
    out: list[dict] = []
    for key, label in (("tax", "Tax"), ("tip", "Tip"), ("service_charge", "Service charge")):
        amount = _to_float(annotation.get(key))
        if amount is not None:
            out.append({"name": label, "translated_name": label, "total": f"{amount:.{dp}f}"})
    for charge in annotation.get("other_charges") or []:
        amount = _to_float(charge.get("amount"))
        if amount is None:
            continue
        out.append({
            "name": charge.get("name") or "Charge",
            "translated_name": charge.get("translated_name") or charge.get("name") or "Charge",
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
        "items_match_receipt_total": None,  # None when receipt_total absent
        "items_receipt_gap": None,
        "ai_vs_server_total_divergence": None,
        "has_tax": False,
        "has_tip": False,
        "has_service_charge": False,
        "other_charges_count": 0,
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
    if receipt_total_f is not None and items_total_f is not None:
        gap = round(items_total_f - receipt_total_f, 6)
        metrics["items_receipt_gap"] = gap
        metrics["items_match_receipt_total"] = abs(gap) < tolerance
    if items_total_f is not None and ai_items_total_f is not None:
        metrics["ai_vs_server_total_divergence"] = (
            abs(items_total_f - ai_items_total_f) > tolerance
        )
    metrics["has_tax"] = annotation.get("tax") is not None
    metrics["has_tip"] = annotation.get("tip") is not None
    metrics["has_service_charge"] = annotation.get("service_charge") is not None
    metrics["other_charges_count"] = len(annotation.get("other_charges") or [])

    return metrics


def _items_receipt_gap(annotation: dict) -> Optional[float]:
    """Absolute gap between the server-calculated items_total and receipt_total
    for an already post-processed annotation. None when either is missing."""
    items_total = _to_float(annotation.get("items_total"))
    receipt_total = _to_float(annotation.get("receipt_total"))
    if items_total is None or receipt_total is None:
        return None
    return abs(items_total - receipt_total)


def select_closest_to_receipt_total(candidates: list[dict]) -> Optional[int]:
    """Given already post-processed annotations, return the index of the one
    whose calculated items_total is closest to its receipt_total. Candidates
    without both totals are excluded. Returns None when no candidate qualifies."""
    best_idx: Optional[int] = None
    best_gap: Optional[float] = None
    for idx, ann in enumerate(candidates):
        if ann is None:
            continue
        gap = _items_receipt_gap(ann)
        if gap is None:
            continue
        if best_gap is None or gap < best_gap:
            best_gap, best_idx = gap, idx
    return best_idx


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
