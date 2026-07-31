"""Checks for the surplus-row repair stage."""

from django.test import SimpleTestCase

from ninjatab.tabs.receipt_scanning.verify import MAX_DROPPED_ROWS, verify_and_repair


def _annotation(items, receipt_total, currency="USD"):
    return {
        "currency_code": currency,
        "receipt_total": receipt_total,
        "items": [{"name": n, "translated_name": n, "total": t} for n, t in items],
    }


class VerifyAndRepairTests(SimpleTestCase):
    def test_noop_when_already_reconciled(self):
        annotation = _annotation([("Coffee", "3.00"), ("Cake", "4.50")], "7.50")
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], 0)
        self.assertTrue(metrics["verify_reconciled_before"])
        self.assertEqual(len(annotation["items"]), 2)

    def test_drops_duplicated_row_carrying_a_copy_of_the_price(self):
        """A wrapped description transcribed as its own row duplicates the price."""
        annotation = _annotation(
            [("Elf Bar 600 Refillable Pod Kit", "5.99"), ("Banana Ice 20mg/ml", "5.99")],
            "5.99",
        )
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], 1)
        self.assertTrue(metrics["verify_reconciled_after"])
        self.assertEqual(len(annotation["items"]), 1)
        self.assertEqual(annotation["items_total"], 5.99)
        self.assertTrue(annotation["totals_reconciled"])

    def test_prefers_dropping_the_continuation_row_not_the_product(self):
        annotation = _annotation(
            [("Pizza Margherita", "12.00"), ("Pizza Margherita large", "12.00")], "12.00"
        )
        verify_and_repair(annotation)
        self.assertEqual([i["name"] for i in annotation["items"]], ["Pizza Margherita"])

    def test_never_drops_when_arithmetic_would_not_come_out_exactly(self):
        """No subset sums to the overshoot, so nothing is touched."""
        annotation = _annotation([("A", "5.00"), ("B", "7.00")], "9.00")
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], 0)
        self.assertEqual(len(annotation["items"]), 2)
        self.assertFalse(metrics["verify_reconciled_after"])

    def test_does_not_repair_an_undershoot(self):
        """Missing rows are not this stage's problem; it must not invent any."""
        annotation = _annotation([("A", "5.00")], "9.00")
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], 0)
        self.assertEqual(len(annotation["items"]), 1)

    def test_missing_receipt_total_is_a_noop(self):
        annotation = _annotation([("A", "5.00"), ("A", "5.00")], None)
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], 0)
        self.assertEqual(len(annotation["items"]), 2)

    def test_never_removes_every_row(self):
        annotation = _annotation([("A", "5.00")], "0.00")
        verify_and_repair(annotation)
        self.assertEqual(len(annotation["items"]), 1)

    def test_bounded_search_does_not_blow_up_on_a_long_receipt(self):
        """40 rows would be 2**40 subsets unbounded; must stay fast and safe."""
        items = [(f"Item {i}", "1.00") for i in range(40)]
        annotation = _annotation(items, "37.00")
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], MAX_DROPPED_ROWS)
        self.assertEqual(len(annotation["items"]), 37)

    def test_respects_zero_decimal_currencies(self):
        annotation = _annotation([("A", "500"), ("A", "500")], "500", currency="JPY")
        metrics = verify_and_repair(annotation)
        self.assertEqual(metrics["verify_rows_dropped"], 1)
        self.assertTrue(metrics["verify_reconciled_after"])

    def test_empty_annotation_is_safe(self):
        self.assertEqual(verify_and_repair({})["verify_rows_dropped"], 0)
        self.assertEqual(verify_and_repair(None)["verify_rows_dropped"], 0)
