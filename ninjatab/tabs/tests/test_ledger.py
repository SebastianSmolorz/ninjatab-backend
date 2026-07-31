"""Checks for ledger reconciliation: items + charges - discounts vs receipt_total."""

from django.test import SimpleTestCase

from ninjatab.tabs.receipt_scanning.ledger import (
    flatten_for_v1,
    item_net_total,
    reconcile_ledger,
    row_category,
)


def _reconcile_v1(annotation, currency="USD"):
    """Reconcile, then present the result the way the shipped mobile client
    reads it. The assertions below are the regression harness for that
    presenter: every hack it performs exists because the client needs it."""
    metrics = reconcile_ledger(annotation, currency)
    flatten_for_v1(annotation)
    return metrics


def _annotation(items, receipt_total, currency="USD", **charges):
    return {
        "currency_code": currency,
        "receipt_total": receipt_total,
        "items": [dict(i) for i in items],
        **charges,
    }


def _item(name, total, **extra):
    return {"name": name, "translated_name": name, "total": total, **extra}


class ItemNetTotalTests(SimpleTestCase):
    def test_plain_item(self):
        self.assertEqual(item_net_total(_item("A", "5.00")), 5.0)

    def test_attached_adjustment_is_not_applied_twice(self):
        """`total` is defined as the final price paid, so a discount the model
        also reported against the item is already inside it."""
        item = _item("A", "3.50", adjustments=[{"name": "Offer", "amount": "-1.50"}])
        self.assertEqual(item_net_total(item), 3.5)

    def test_is_new_price_adjustment_does_not_override_the_total(self):
        item = _item(
            "A", "3.00",
            adjustments=[{"name": "Was/now", "amount": "3.00", "is_new_price": True}],
        )
        self.assertEqual(item_net_total(item), 3.0)


class RowCategoryTests(SimpleTestCase):
    def test_prefers_the_models_kind(self):
        # The name says nothing; the model's kind carries the classification.
        self.assertEqual(row_category(_item("Bediening", "4.00", kind="service")), "service")

    def test_unfoldable_discount_is_spread_not_offered_as_an_item(self):
        """A credit nobody ordered must never be an evenly-split line."""
        self.assertEqual(row_category(_item("Offer", "-1.00", kind="discount")), "service")

    def test_any_negative_row_is_treated_as_a_credit(self):
        self.assertEqual(row_category(_item("PRYMAT 3 FOR 1.20", "-0.27")), "service")

    def test_falls_back_to_the_name_when_unclassified(self):
        self.assertEqual(row_category(_item("Service Charge", "4.00")), "service")
        self.assertEqual(row_category(_item("Sticky Toffee", "4.00")), "item")


class ReconcileLedgerV1Tests(SimpleTestCase):
    """The flat list the shipped client consumes, via `flatten_for_v1`."""

    def test_additive_charge_is_added_and_reaches_the_total(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00", service_charge="3.00"
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual(metrics["charges_additive_count"], 1)
        # The client's flat list carries the charge with its category.
        self.assertEqual(
            [(i["total"], i["category"]) for i in annotation["items"]],
            [("20.00", "item"), ("3.00", "service")],
        )

    def test_inclusive_vat_is_not_added(self):
        """Prices already include the tax, so adding it would double-charge."""
        annotation = _annotation([_item("Pasta", "20.00")], "20.00", tax="3.33")
        metrics = _reconcile_v1(annotation, "USD")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual(metrics["charges_additive_count"], 0)
        self.assertEqual(len(annotation["items"]), 1)

    def test_picks_only_the_charges_that_close_the_gap(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "22.00", tax="5.00", service_charge="2.00"
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual(
            [i["category"] for i in annotation["items"]], ["item", "service"]
        )

    def test_standalone_discount_row_stays_beneath_its_item(self):
        annotation = _annotation(
            [_item("Crisps", "3.00"), _item("3 for 2", "-1.00", kind="discount")],
            "2.00",
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual(
            [(i["total"], i["category"]) for i in annotation["items"]],
            [("3.00", "item"), ("-1.00", "service")],
        )

    def test_attached_discount_becomes_its_own_row_under_the_item(self):
        """The client has no concept of an adjustment, so the discount is shown
        as a negative row and the item grossed back up to match."""
        annotation = _annotation(
            [_item("Crisps", "2.00", adjustments=[{"name": "3 for 2", "amount": "-1.00"}])],
            "2.00",
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual(metrics["discount_rows_expanded"], 1)
        self.assertEqual(
            [(i["name"], i["total"], i["category"]) for i in annotation["items"]],
            [("Crisps", "3.00", "item"), ("3 for 2", "-1.00", "service")],
        )

    def test_discount_carrier_row_does_not_leave_an_empty_item(self):
        """Some parses emit a coupon line that also lists itself as its own
        adjustment; grossing that up would leave a 0.00 item beside it."""
        annotation = _annotation(
            [_item("Coupon", "-5.00", kind="discount",
                   adjustments=[{"name": "Coupon", "amount": "-5.00"}])],
            "-5.00",
        )
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["total"] for i in annotation["items"]], ["-5.00"])

    def test_zero_adjustments_do_not_emit_a_row(self):
        annotation = _annotation(
            [_item("Crisps", "2.00", adjustments=[{"name": "None", "amount": "0"}])],
            "2.00",
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertEqual(metrics["discount_rows_expanded"], 0)
        self.assertEqual([i["total"] for i in annotation["items"]], ["2.00"])

    def test_kinds_are_ignored_when_they_would_empty_the_receipt(self):
        """Every row classified as a charge is never a real receipt."""
        annotation = _annotation([_item("Kviitung", "19.50", kind="service")], "19.50")
        metrics = _reconcile_v1(annotation, "USD")
        self.assertTrue(metrics.get("kinds_overridden"))
        self.assertEqual([i["category"] for i in annotation["items"]], ["item"])
        self.assertTrue(metrics["items_match_receipt_total"])

    def test_refuses_to_reconcile_by_emptying_the_receipt(self):
        """A charge worth the whole bill over no items is not a real receipt."""
        annotation = _annotation(
            [_item("Nothing", "0.00")], "500.00",
            other_charges=[{"name": "Service Fee", "translated_name": "Service Fee",
                            "amount": "500.00"}],
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertEqual(metrics["charges_additive_count"], 0)
        self.assertFalse(metrics["items_match_receipt_total"])

    def test_unreconcilable_charges_are_not_added(self):
        annotation = _annotation([_item("Pasta", "20.00")], "99.00", tax="3.00")
        metrics = _reconcile_v1(annotation, "USD")
        self.assertEqual(metrics["charge_selection"], "unreconciled_none_added")
        self.assertEqual(metrics["charges_additive_count"], 0)
        self.assertFalse(annotation["totals_reconciled"])

    def test_missing_receipt_total_adds_no_charges(self):
        annotation = _annotation([_item("Pasta", "20.00")], None, tax="3.00")
        metrics = _reconcile_v1(annotation, "USD")
        self.assertEqual(metrics["charge_selection"], "no_receipt_total")
        self.assertIsNone(metrics["items_match_receipt_total"])

    def test_zero_decimal_currency_reconciles(self):
        annotation = _annotation([_item("Ramen", "900")], "1000", currency="JPY",
                                 service_charge="100")
        metrics = _reconcile_v1(annotation, "JPY")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual([i["total"] for i in annotation["items"]], ["900", "100"])

    def test_tax_only_comes_from_the_model_not_from_a_name(self):
        """An amount the model filed as a generic charge is not tax just because
        it is named like one — we never made that classification."""
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00",
            other_charges=[{"name": "MwSt 19%", "translated_name": "MwSt 19%",
                            "amount": "3.00"}],
        )
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "item"])

    def test_an_unclassified_fee_becomes_an_ordinary_line(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00",
            other_charges=[{"name": "DELIVERY CHARGE", "translated_name": "Delivery charge",
                            "amount": "3.00"}],
        )
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "item"])

    def test_explicit_service_charge_still_splits_by_spend(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00", service_charge="3.00"
        )
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "service"])

    def test_only_the_largest_tax_keeps_the_tax_category(self):
        """The client shows one tax and rewrites all of them on edit, so a second
        tax row must not stay a tax — but its money must stay in the bill."""
        annotation = _annotation(
            [_item("Pasta", "20.00", kind="item"),
             _item("VAT 20%", "4.00", kind="tax"),
             _item("VAT 5%", "1.00", kind="tax")],
            "25.00",
        )
        metrics = _reconcile_v1(annotation, "USD")
        self.assertEqual(metrics["duplicate_taxes_demoted"], 1)
        self.assertEqual(
            [(i["total"], i["category"]) for i in annotation["items"]],
            [("20.00", "item"), ("4.00", "tax"), ("1.00", "item")],
        )
        self.assertTrue(metrics["items_match_receipt_total"])

    def test_tip_in_other_charges_is_still_a_tip(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00",
            other_charges=[{"name": "Propina", "translated_name": "Propina",
                            "amount": "3.00"}],
        )
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "tip"])

    def test_negative_charge_is_a_credit_not_a_tax(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "19.55",
            other_charges=[{"name": "Multi-save", "translated_name": "Multi-save",
                            "amount": "-0.45"}],
        )
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "service"])

    def test_model_stated_kinds_still_win_over_the_default(self):
        annotation = _annotation([_item("Pasta", "20.00")], "23.00", tip="3.00")
        _reconcile_v1(annotation, "USD")
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "tip"])

    def test_v1_still_shows_one_tax_where_v2_shows_two(self):
        """The presenter is where the client's one-tax limit lives, not the
        ledger — the second tax stays a tax right up until v1 flattens it."""
        annotation = _annotation(
            [_item("Pasta", "20.00"), _item("VAT 20%", "4.00", kind="tax"),
             _item("VAT 5%", "1.00", kind="tax")],
            "25.00",
        )
        reconcile_ledger(annotation, "USD")
        self.assertEqual(
            [a["type"] for a in annotation["adjustments"]], ["tax", "tax"]
        )
        flatten_for_v1(annotation)
        self.assertEqual(
            [i["category"] for i in annotation["items"]], ["item", "tax", "item"]
        )

    def test_client_contract_is_preserved(self):
        """Every row the client reads must still be present and shaped as before."""
        annotation = _annotation([_item("Pasta", "20.00")], "23.00", tip="3.00")
        _reconcile_v1(annotation, "USD")
        for row in annotation["items"]:
            self.assertIn("name", row)
            self.assertIn("translated_name", row)
            self.assertIn("total", row)
            self.assertIn(row["category"], {"item", "tax", "tip", "service"})
        self.assertIn("receipt_total", annotation)
        self.assertIn("currency_code", annotation)


class LedgerFormTests(SimpleTestCase):
    """The v2 shape: what was ordered stays in `items`, everything charged on
    top of it goes to `adjustments` with the split mode it defaults to."""

    def test_charges_leave_the_item_list(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00", service_charge="3.00"
        )
        reconcile_ledger(annotation, "USD")
        self.assertEqual([i["total"] for i in annotation["items"]], ["20.00"])
        self.assertEqual(
            [(a["amount"], a["type"], a["split"]) for a in annotation["adjustments"]],
            [("3.00", "service", "proportional")],
        )
        self.assertEqual(annotation["grand_total"], 23.0)

    def test_a_fee_is_shared_equally_and_a_tip_is_not(self):
        annotation = _annotation(
            [_item("Pasta", "20.00")], "26.00", tip="3.00",
            other_charges=[{"name": "Delivery", "translated_name": "Delivery",
                            "amount": "3.00", "type": "fee"}],
        )
        reconcile_ledger(annotation, "USD")
        self.assertEqual(
            {(a["type"], a["split"]) for a in annotation["adjustments"]},
            {("tip", "proportional"), ("fee", "even")},
        )

    def test_the_models_type_classifies_a_charge_it_filed_generically(self):
        """A tax the model put in other_charges is a tax in v2 — it said so —
        while v1 keeps treating it as an ordinary line, unchanged."""
        annotation = _annotation(
            [_item("Pasta", "20.00")], "23.00",
            other_charges=[{"name": "MwSt 19%", "translated_name": "MwSt 19%",
                            "amount": "3.00", "type": "tax"}],
        )
        reconcile_ledger(annotation, "USD")
        self.assertEqual([a["type"] for a in annotation["adjustments"]], ["tax"])
        flatten_for_v1(annotation)
        self.assertEqual([i["category"] for i in annotation["items"]], ["item", "item"])

    def test_an_item_discount_stays_on_its_item(self):
        """No gross-up and no negative row: the client that needed those is v1."""
        annotation = _annotation(
            [_item("Crisps", "2.00", adjustments=[{"name": "3 for 2", "amount": "-1.00"}])],
            "2.00",
        )
        reconcile_ledger(annotation, "USD")
        self.assertEqual(len(annotation["items"]), 1)
        self.assertEqual(annotation["items"][0]["total"], "2.00")
        self.assertEqual(
            annotation["items"][0]["adjustments"], [{"name": "3 for 2", "amount": "-1.00"}]
        )
        self.assertEqual(annotation["adjustments"], [])

    def test_the_bill_adds_up_across_both_lists(self):
        annotation = _annotation(
            [_item("Pasta", "20.00"), _item("Wine", "10.00")], "34.50",
            tax="2.50", service_charge="2.00",
        )
        metrics = reconcile_ledger(annotation, "USD")
        self.assertTrue(metrics["items_match_receipt_total"])
        self.assertEqual(annotation["items_total"], 30.0)
        self.assertEqual(annotation["adjustments_total"], 4.5)
        self.assertEqual(annotation["grand_total"], 34.5)

    def test_flattening_a_flat_annotation_is_a_no_op(self):
        annotation = {"items": [_item("Pasta", "20.00")], "items_total": 20.0}
        self.assertEqual(flatten_for_v1(annotation), annotation)
        self.assertIsNone(flatten_for_v1(None))
