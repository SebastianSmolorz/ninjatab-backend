"""A zero-value row is neither a hit nor a miss for the amount metrics."""

import receipt_validation  # noqa: F401  (sys.path shim)
from labeler.evaluation.scorer import score_against_label

LABEL = {
    "items": [{"name": "Flat White", "total": "3.60"},
              {"name": "Chocolate", "total": "3.40"}],
    "receipt_total": "7.00",
}


def _result(items):
    return {"items": items, "adjustments": [], "receipt_total": "7.00"}


def test_zero_row_costs_nothing():
    clean = _result([{"total": "3.60", "category": "item"},
                     {"total": "3.40", "category": "item"}])
    with_zero = _result([{"total": "0", "category": "item"},
                         {"total": "3.60", "category": "item"},
                         {"total": "3.40", "category": "item"}])
    assert score_against_label(clean, LABEL)["p1_item_totals_f1"] == 1.0
    assert score_against_label(with_zero, LABEL)["p1_item_totals_f1"] == 1.0
    assert score_against_label(with_zero, LABEL)["p1_item_count_exact"] is True


def test_a_wrong_amount_still_scores():
    wrong = _result([{"total": "3.60", "category": "item"},
                     {"total": "9.99", "category": "item"}])
    assert score_against_label(wrong, LABEL)["p1_item_totals_f1"] == 0.5
