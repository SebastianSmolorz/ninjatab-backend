"""Checks for consensus candidate selection."""

from django.test import SimpleTestCase

from ninjatab.tabs.receipt_scanning.postprocess import (
    drops_rows,
    select_best_line_items,
)


def _candidate(totals, receipt_total, ai_items_total=None):
    items = [{"name": f"Item {i}", "total": f"{t:.2f}"} for i, t in enumerate(totals)]
    return {
        "currency_code": "GBP",
        "receipt_total": f"{receipt_total:.2f}",
        "items": items,
        "items_total": round(sum(totals), 2),
        "ai_items_total": ai_items_total,
    }


class SelectBestLineItemsTests(SimpleTestCase):
    def test_distinct_item_counts_fall_through_to_the_gap(self):
        """A mode of one is not a mode: with 1/2/3 rows nothing is modal, so the
        candidate closest to receipt_total wins rather than whichever came first.
        """
        candidates = [
            _candidate([25.15], 109.48),
            _candidate([14.00, 14.15], 109.48),
            _candidate([30.10, 30.10, 30.09], 109.48),
        ]
        self.assertEqual(select_best_line_items(candidates), 2)

    def test_a_real_mode_still_beats_a_smaller_gap(self):
        """Two candidates agreeing on row count outrank one that merged rows to
        land nearer the total."""
        candidates = [
            _candidate([10.00], 20.50),
            _candidate([10.00, 10.00], 20.50),
            _candidate([10.00, 10.00], 20.50),
        ]
        self.assertIn(select_best_line_items(candidates), (1, 2))


class DropsRowsTests(SimpleTestCase):
    def test_rows_short_of_the_models_own_subtotal(self):
        self.assertTrue(drops_rows(_candidate([25.15], 109.48, ai_items_total=30.05)))

    def test_rows_matching_the_subtotal_are_clean(self):
        self.assertFalse(drops_rows(_candidate([25.15], 109.48, ai_items_total=25.15)))

    def test_a_surplus_is_not_a_drop(self):
        self.assertFalse(drops_rows(_candidate([40.00], 109.48, ai_items_total=30.05)))

    def test_no_subtotal_is_no_signal(self):
        self.assertFalse(drops_rows(_candidate([25.15], 109.48)))
