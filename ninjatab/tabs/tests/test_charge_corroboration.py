"""Checks that a charge no second OCR call saw is not trusted.

Inventing a charge costs the payer money, and a missing item is arithmetically
indistinguishable from a phantom charge of the same size, so reconciliation
alone cannot catch it.
"""

from django.test import SimpleTestCase

from ninjatab.tabs.receipt_scanning.strategies import _drop_uncorroborated_charges


def _ann(**kwargs):
    return {"items": [{"name": "A", "translated_name": "A", "total": "10.00"}], **kwargs}


class DropUncorroboratedChargesTests(SimpleTestCase):
    def test_charge_seen_by_one_call_only_is_dropped(self):
        annotations = [_ann(tax="12.00"), _ann(), _ann()]
        dropped = _drop_uncorroborated_charges(annotations)
        self.assertEqual(dropped, 1)
        self.assertIsNone(annotations[0]["tax"])

    def test_charge_seen_twice_is_kept(self):
        annotations = [_ann(tax="12.00"), _ann(tax="12.00"), _ann()]
        self.assertEqual(_drop_uncorroborated_charges(annotations), 0)
        self.assertEqual(annotations[0]["tax"], "12.00")

    def test_corroboration_may_come_from_a_different_field(self):
        """The same figure read as tax by one call and service by another is
        still a figure two calls agree is on the receipt."""
        annotations = [_ann(tax="3.00"), _ann(service_charge="3.00"), _ann()]
        self.assertEqual(_drop_uncorroborated_charges(annotations), 0)

    def test_other_charges_entries_are_filtered_too(self):
        annotations = [
            _ann(other_charges=[{"name": "Delivery", "amount": "5.00"},
                                {"name": "Ghost", "amount": "99.00"}]),
            _ann(other_charges=[{"name": "Delivery", "amount": "5.00"}]),
        ]
        self.assertEqual(_drop_uncorroborated_charges(annotations), 1)
        self.assertEqual(
            [c["amount"] for c in annotations[0]["other_charges"]], ["5.00"]
        )

    def test_a_single_reading_is_left_alone(self):
        """With nothing to compare against, dropping would only lose data."""
        annotations = [_ann(tax="12.00")]
        self.assertEqual(_drop_uncorroborated_charges(annotations), 0)
        self.assertEqual(annotations[0]["tax"], "12.00")

    def test_one_candidate_cannot_corroborate_itself(self):
        annotations = [
            _ann(tax="7.00", other_charges=[{"name": "Dup", "amount": "7.00"}]),
            _ann(),
        ]
        self.assertEqual(_drop_uncorroborated_charges(annotations), 2)

    def test_missing_annotations_are_tolerated(self):
        annotations = [None, _ann(tax="12.00"), None]
        self.assertEqual(_drop_uncorroborated_charges(annotations), 0)
