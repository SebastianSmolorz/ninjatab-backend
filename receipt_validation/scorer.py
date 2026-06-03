import difflib
from datetime import date, datetime
from typing import Optional

from ninjatab.tabs.receipt_scanning.postprocess import _to_float

# Relative importance of each metric in the rolled-up ``total_score``. Financial
# correctness dominates, but item identification (name + per-item total) matters
# for a bill-splitting app, so it carries real weight too. A weight of 0 keeps a
# metric visible in the breakdown but out of the rollup. Edit freely — only the
# rollup is affected, individual metrics are untouched.
WEIGHTS = {
    "receipt_total_accuracy": 3.0,
    "items_total_accuracy": 1.0,
    "item_count_match": 2.0,
    "items_sum_vs_receipt_total": 0.0,  # redundant + wrong when tax/charges exist
    "items_sum_vs_items_total": 2.0,
    "item_total_accuracy": 2.0,
    "item_name_fuzzy_match": 2.5,
    "item_translated_name_fuzzy_match": 1.5,
    "item_quantity_accuracy": 0.5,
    "item_price_per_quantity_accuracy": 0.5,
    "currency_match": 0.5,
    "language_match": 0.5,
    "date_match": 0.5,
}


def _total_accuracy(got, expected) -> Optional[float]:
    got = _to_float(got)
    expected = _to_float(expected)
    if expected is None or got is None:
        return None
    if expected == 0:
        return 1.0 if got == 0 else 0.0
    return max(0.0, 1.0 - abs(got - expected) / abs(expected))


def _fuzzy(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _item_count_score(result_items: list, expected_items: list) -> float:
    """Graded item-count score: 1.0 when counts match, falling off with an
    *escalating* penalty as the gap grows. The k-th miscounted item costs k
    penalty units (so being off by several items hurts disproportionately),
    normalised by the expected item count. A miscounted item whose total is zero
    (e.g. a deposit/section line) incurs only half the penalty. The discrepant
    items are taken as the cheapest on whichever side has too many/few, which is
    where spurious or dropped lines usually sit."""
    n_e = len(expected_items)
    n_r = len(result_items)
    if n_e == 0:
        return 1.0 if n_r == 0 else 0.0
    diff = abs(n_r - n_e)
    if diff == 0:
        return 1.0

    side = result_items if n_r > n_e else expected_items
    costs = sorted(abs(_to_float(i.get("total")) or 0.0) for i in side)
    discrepant = costs[:diff]

    penalty = 0.0
    for k, cost in enumerate(discrepant, start=1):
        unit = 0.5 if cost == 0.0 else 1.0
        penalty += k * unit
    penalty /= n_e
    return max(0.0, 1.0 - penalty)


def _align_items(expected_items: list, result_items: list) -> list[tuple[dict, Optional[dict]]]:
    """Greedy one-to-one alignment of each expected item to its best-matching
    (by name) result item. Each result item is consumed at most once, so a
    single good result name can't be credited to several expected items.
    Returns (expected_item, result_item_or_None) for every expected item."""
    remaining = list(range(len(result_items)))
    pairs = []
    for exp in expected_items:
        exp_name = exp.get("name") or ""
        best_j, best_score = None, -1.0
        for j in remaining:
            score = _fuzzy(exp_name, result_items[j].get("name") or "")
            if score > best_score:
                best_score, best_j = score, j
        if best_j is not None:
            remaining.remove(best_j)
            pairs.append((exp, result_items[best_j]))
        else:
            pairs.append((exp, None))
    return pairs


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _aligned_text(pairs: list, key: str) -> Optional[float]:
    """Partial credit per item: fuzzy ratio of the aligned result text to the
    expected text (0 when the expected item went unmatched). Averaged over the
    expected items that actually carry text for ``key``."""
    scores = []
    for exp, res in pairs:
        expected_text = exp.get(key)
        if not expected_text:
            continue
        scores.append(0.0 if res is None else _fuzzy(expected_text, res.get(key) or ""))
    return _avg(scores)


def _aligned_numeric(pairs: list, key: str) -> Optional[float]:
    """Per-item numeric accuracy of ``key`` over aligned pairs (0 when the
    expected item went unmatched or the result omitted the field). Averaged over
    the expected items that carry a value for ``key``."""
    scores = []
    for exp, res in pairs:
        if _to_float(exp.get(key)) is None:
            continue
        if res is None:
            scores.append(0.0)
        else:
            acc = _total_accuracy(res.get(key), exp.get(key))
            scores.append(acc if acc is not None else 0.0)
    return _avg(scores)


def score_result(result: dict, expected: dict) -> dict:
    """Compare a strategy result against expected ground truth."""
    result_ann = (result or {}).get("document_annotation") or {}
    expected_ann = (expected or {}).get("document_annotation") or {}

    result_items = result_ann.get("items") or []
    expected_items = expected_ann.get("items") or []

    # Graded, escalating item-count score (zero-cost items penalised at half).
    item_count_match = _item_count_score(result_items, expected_items)

    # Receipt total accuracy (model-reported grand total)
    receipt_total_accuracy = _total_accuracy(
        result_ann.get("receipt_total"),
        expected_ann.get("receipt_total"),
    )

    # Items total accuracy (model-reported subtotal)
    items_total_accuracy = _total_accuracy(
        result_ann.get("items_total"),
        expected_ann.get("items_total"),
    )

    # Sum of result item totals vs receipt_total and items_total
    result_item_totals = [_to_float(i.get("total")) for i in result_items]
    result_items_sum = sum(t for t in result_item_totals if t is not None)
    items_sum_vs_receipt_total = _total_accuracy(result_items_sum, expected_ann.get("receipt_total"))
    items_sum_vs_items_total = _total_accuracy(result_items_sum, expected_ann.get("items_total"))

    # Per-item metrics over a one-to-one name alignment: partial credit for item
    # text (name + translation) and per-item numeric fields.
    pairs = _align_items(expected_items, result_items)
    item_name_fuzzy_match = _aligned_text(pairs, "name")
    item_translated_name_fuzzy_match = _aligned_text(pairs, "translated_name")
    item_total_accuracy = _aligned_numeric(pairs, "total")
    item_quantity_accuracy = _aligned_numeric(pairs, "quantity")
    item_price_per_quantity_accuracy = _aligned_numeric(pairs, "price_per_quantity")

    # Currency code match (exact, case-insensitive)
    expected_currency = (expected_ann.get("currency_code") or "").upper()
    result_currency = (result_ann.get("currency_code") or "").upper()
    currency_match = (
        1.0 if expected_currency and result_currency == expected_currency
        else 0.0 if expected_currency
        else None
    )

    # Language match (exact, case-insensitive)
    expected_lang = (expected_ann.get("receipt_language") or "").lower()
    result_lang = (result_ann.get("receipt_language") or "").lower()
    language_match = (
        1.0 if expected_lang and result_lang == expected_lang
        else 0.0 if expected_lang
        else None
    )

    expected_date = _parse_date(expected_ann.get("datetime_of_receipt"))
    result_date = _parse_date(result_ann.get("datetime_of_receipt"))
    date_match = (
        1.0 if expected_date and result_date == expected_date
        else 0.0 if expected_date
        else None
    )

    individual = {
        "item_count_match": item_count_match,
        "items_sum_vs_receipt_total": items_sum_vs_receipt_total,
        "items_sum_vs_items_total": items_sum_vs_items_total,
        "receipt_total_accuracy": receipt_total_accuracy,
        "items_total_accuracy": items_total_accuracy,
        "item_total_accuracy": item_total_accuracy,
        "item_name_fuzzy_match": item_name_fuzzy_match,
        "item_translated_name_fuzzy_match": item_translated_name_fuzzy_match,
        "item_quantity_accuracy": item_quantity_accuracy,
        "item_price_per_quantity_accuracy": item_price_per_quantity_accuracy,
        "currency_match": currency_match,
        "language_match": language_match,
        "date_match": date_match,
    }
    # Weighted average over the metrics that are present (non-None) for this case.
    weighted = [
        (WEIGHTS.get(k, 1.0), v) for k, v in individual.items() if v is not None
    ]
    weight_sum = sum(w for w, _ in weighted)
    total_score = sum(w * v for w, v in weighted) / weight_sum if weight_sum else None
    return {"total_score": total_score, **individual}
