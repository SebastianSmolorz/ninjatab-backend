import difflib
from datetime import date, datetime
from typing import Optional


def _total_accuracy(got: Optional[float], expected: Optional[float]) -> Optional[float]:
    if expected is None or got is None:
        return None
    if expected == 0:
        return 1.0 if got == 0 else 0.0
    return max(0.0, 1.0 - abs(got - expected) / abs(expected))


def _best_fuzzy_match(name: str, candidates: list[str]) -> float:
    if not candidates:
        return 0.0
    name_lower = name.lower()
    return max(
        difflib.SequenceMatcher(None, name_lower, c.lower()).ratio()
        for c in candidates
    )


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _avg_fuzzy(expected_names: list[str], result_names: list[str]) -> Optional[float]:
    if not expected_names:
        return None
    scores = [_best_fuzzy_match(n, result_names) for n in expected_names if n]
    return sum(scores) / len(scores) if scores else None


def score_result(result: dict, expected: dict) -> dict:
    """Compare a strategy result against expected ground truth."""
    result_ann = (result or {}).get("document_annotation") or {}
    expected_ann = (expected or {}).get("document_annotation") or {}

    result_items = result_ann.get("items") or []
    expected_items = expected_ann.get("items") or []

    # Item count match
    item_count_match = 1.0 if len(result_items) == len(expected_items) else 0.0

    # Receipt total accuracy
    receipt_total_accuracy = _total_accuracy(
        result_ann.get("receipt_total"),
        expected_ann.get("receipt_total"),
    )

    # Items total accuracy
    items_total_accuracy = _total_accuracy(
        result_ann.get("items_total"),
        expected_ann.get("items_total"),
    )

    # Sum of result item totals vs receipt_total and items_total
    result_item_totals = [i["total"] for i in result_items if i.get("total") is not None]
    result_items_sum = sum(result_item_totals) if result_item_totals else 0.0
    items_sum_vs_receipt_total = _total_accuracy(result_items_sum, expected_ann.get("receipt_total"))
    items_sum_vs_items_total = _total_accuracy(result_items_sum, expected_ann.get("items_total"))

    # Item name fuzzy match (original receipt language names)
    result_names = [i.get("name") or "" for i in result_items]
    expected_names = [i.get("name") or "" for i in expected_items]
    item_name_fuzzy_match = _avg_fuzzy(expected_names, result_names)

    # Translated name fuzzy match (English names)
    result_translated = [i.get("translated_name") or "" for i in result_items]
    expected_translated = [i.get("translated_name") or "" for i in expected_items]
    item_translated_name_fuzzy_match = _avg_fuzzy(expected_translated, result_translated)

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
        "item_name_fuzzy_match": item_name_fuzzy_match,
        "item_translated_name_fuzzy_match": item_translated_name_fuzzy_match,
        "currency_match": currency_match,
        "language_match": language_match,
        "date_match": date_match,
    }
    scored = [v for v in individual.values() if v is not None]
    total_score = sum(scored) / len(scored) if scored else None
    return {"total_score": total_score, **individual}
