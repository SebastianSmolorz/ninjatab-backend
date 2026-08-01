"""Checks for the per-role charge metrics (p2_tax_f1 / p2_tip_f1).

The scorer lives in the labeller repo; importing `receipt_validation` first is
what puts the repo root on sys.path.
"""

import receipt_validation  # noqa: F401  (sys.path shim)
from labeler.evaluation.scorer import result_amounts_by_role, score_against_label

LABEL = {
    "items": [{"name": "Burger", "total": "10.00"}],
    "charges": [
        {"name": "Sales Tax", "amount": "1.00", "charge_type": "tax"},
        {"name": "Gratuity", "amount": "2.00", "charge_type": "service"},
    ],
    "receipt_total": "13.00",
}


def _v2(tax_amount, tip_amount):
    return {
        "items": [{"name": "Burger", "total": "10.00", "category": "item"}],
        "adjustments": [
            {"name": "Tax", "amount": tax_amount, "type": "tax"},
            {"name": "Tip", "amount": tip_amount, "type": "tip"},
        ],
        "receipt_total": "13.00",
    }


def test_roles_read_from_both_forms():
    v1 = {
        "items": [
            {"total": "10.00", "category": "item"},
            {"total": "1.00", "category": "tax"},
            {"total": "2.00", "category": "tip"},
        ]
    }
    assert result_amounts_by_role(v1) == result_amounts_by_role(_v2("1.00", "2.00"))


def test_swapped_roles_hide_in_the_combined_metric():
    """The whole reason the split exists: 1.00 and 2.00 are both present, so
    p2_charges_f1 is perfect while each role is half wrong."""
    swapped = score_against_label(_v2("2.00", "1.00"), LABEL)
    assert swapped["p2_charges_f1"] == 1.0
    assert swapped["p2_tax_f1"] == 0.0
    assert swapped["p2_tip_f1"] == 0.0

    correct = score_against_label(_v2("1.00", "2.00"), LABEL)
    assert correct["p2_tax_f1"] == 1.0
    assert correct["p2_tip_f1"] == 1.0


def test_absent_on_both_sides_is_not_scored():
    """A receipt with no tip must not hand out a free 1.0 - or a free 0.0."""
    label = {**LABEL, "charges": LABEL["charges"][:1], "receipt_total": "11.00"}
    metrics = score_against_label(
        {
            "items": [{"name": "Burger", "total": "10.00", "category": "item"}],
            "adjustments": [{"name": "Tax", "amount": "1.00", "type": "tax"}],
            "receipt_total": "11.00",
        },
        label,
    )
    assert metrics["p2_tax_f1"] == 1.0
    assert metrics["p2_tip_f1"] is None
