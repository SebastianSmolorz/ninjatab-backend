"""Unit tests for the v2 receipt post-processing ledger (charges/subtotal/
adjustments). Pure-function tests — no DB."""

from ninjatab.tabs.receipt_scanning.postprocess import (
    _fold_item_adjustments,
    _is_reconciled,
    _items_receipt_gap,
    _normalize_amounts_in_annotation,
    _reconcile_ledger,
    flatten_for_legacy,
    standard_post_process,
)


def item(name, total, **kw):
    return {"name": name, "translated_name": name, "total": total, **kw}


def charge(name, amount, kind="charge", included=False):
    return {
        "name": name,
        "translated_name": name,
        "kind": kind,
        "amount": amount,
        "included_in_item_totals": included,
    }


# --- _fold_item_adjustments -------------------------------------------------

def test_fold_signed_delta():
    ann = {
        "currency_code": "GBP",
        "items": [item("Pizza", "10.00", adjustments=[
            {"name": "discount", "translated_name": "discount", "amount": "-2.00", "is_new_price": False},
        ])],
    }
    assert _fold_item_adjustments(ann) == 1
    assert ann["items"][0]["total"] == "8.00"
    assert ann["items"][0]["gross_total"] == "10.00"


def test_fold_replacement_price_wins_over_deltas():
    ann = {
        "currency_code": "GBP",
        "items": [item("Beers", "6.00", adjustments=[
            {"name": "-0.50", "translated_name": "-0.50", "amount": "-0.50", "is_new_price": False},
            {"name": "3 for £3", "translated_name": "3 for £3", "amount": "3.00", "is_new_price": True},
        ])],
    }
    assert _fold_item_adjustments(ann) == 1
    assert ann["items"][0]["total"] == "3.00"


def test_fold_noop_when_net_equals_gross():
    ann = {
        "currency_code": "GBP",
        "items": [item("Tea", "2.00", adjustments=[
            {"name": "note", "translated_name": "note", "amount": "0.00", "is_new_price": False},
        ])],
    }
    assert _fold_item_adjustments(ann) == 0
    assert ann["items"][0]["total"] == "2.00"
    assert "gross_total" not in ann["items"][0]


# --- _reconcile_ledger ------------------------------------------------------

def test_ledger_already_reconciled_is_untouched():
    ann = {
        "currency_code": "USD",
        "items": [item("Burger", "10.00")],
        "charges": [charge("Sales tax", "0.80")],
        "receipt_total": "10.80",
    }
    assert _reconcile_ledger(ann) == "none"
    assert ann["charges"][0]["included_in_item_totals"] is False


def test_flip_included_repair():
    # EU receipt: items already sum to the gross total, but the model marked
    # the VAT line as adding on top (the probe-case failure).
    ann = {
        "currency_code": "EUR",
        "items": [item("Dinner", "233.30")],
        "charges": [charge("VAT 24%", "45.15")],
        "receipt_total": "233.30",
    }
    assert _reconcile_ledger(ann) == "flip_included"
    assert ann["charges"][0]["included_in_item_totals"] is True


def test_flip_included_other_direction():
    # Model marked a contributing tip as included; ledger comes up short.
    ann = {
        "currency_code": "USD",
        "items": [item("Steak", "40.00")],
        "charges": [charge("Gratuity", "8.00", kind="tip", included=True)],
        "receipt_total": "48.00",
    }
    assert _reconcile_ledger(ann) == "flip_included"
    assert ann["charges"][0]["included_in_item_totals"] is False


def test_reclassify_misrouted_item():
    # A VAT row emitted as an item inflates the items sum.
    ann = {
        "currency_code": "EUR",
        "items": [item("Wine", "20.00"), item("VAT 19%", "3.80")],
        "receipt_total": "20.00",
    }
    assert _reconcile_ledger(ann) == "reclassified_item"
    assert [i["name"] for i in ann["items"]] == ["Wine"]
    assert ann["charges"][0]["name"] == "VAT 19%"
    assert ann["charges"][0]["included_in_item_totals"] is True


def test_unfold_repair_when_model_reported_net():
    # Model returned the already-discounted total AND the adjustment; the fold
    # double-subtracted, so reverting it reconciles.
    ann = {
        "currency_code": "GBP",
        "items": [item("Pizza", "8.00", adjustments=[
            {"name": "discount", "translated_name": "discount", "amount": "-2.00", "is_new_price": False},
        ])],
        "receipt_total": "8.00",
    }
    _fold_item_adjustments(ann)
    assert ann["items"][0]["total"] == "6.00"
    assert _reconcile_ledger(ann) == "unfolded_adjustments"
    assert ann["items"][0]["total"] == "8.00"
    assert "gross_total" not in ann["items"][0]


def test_unrepairable_leaves_data_untouched():
    ann = {
        "currency_code": "USD",
        "items": [item("Salad", "7.00")],
        "receipt_total": "99.99",
    }
    assert _reconcile_ledger(ann) == "none"
    assert ann["items"][0]["total"] == "7.00"


def test_repair_must_not_break_holding_subtotal():
    # Reclassifying "Service station pie" would fix I2, but subtotal pins the
    # items sum, so the repair is rejected.
    ann = {
        "currency_code": "GBP",
        "items": [item("Pie", "5.00"), item("Service fee", "1.00")],
        "subtotal": "6.00",
        "receipt_total": "5.00",
    }
    assert _reconcile_ledger(ann) == "none"
    assert len(ann["items"]) == 2


# --- flatten_for_legacy -----------------------------------------------------

def test_flatten_folds_non_included_charges():
    ann = {
        "currency_code": "USD",
        "items": [item("Burger", "10.00", adjustments=[
            {"name": "deal", "translated_name": "deal", "amount": "-1.00", "is_new_price": False},
        ], gross_total="11.00")],
        "subtotal": "10.00",
        "charges": [
            charge("Sales tax", "0.80"),
            charge("Tip", "2.00", kind="tip"),
            charge("VAT info", "1.50", included=True),
        ],
        "receipt_total": "12.80",
    }
    flat = flatten_for_legacy(ann)
    names = [(i["name"], i.get("category")) for i in flat["items"]]
    assert names == [("Burger", None), ("Sales tax", "tax"), ("Tip", "tip")]
    assert flat["tax"] == "0.80"
    assert flat["tip"] == "2.00"
    assert flat["items_total"] == 12.80
    assert "charges" not in flat
    assert "subtotal" not in flat
    assert "adjustments" not in flat["items"][0]
    assert "gross_total" not in flat["items"][0]
    # Original untouched
    assert "charges" in ann


# --- normalization ----------------------------------------------------------

def test_normalize_covers_new_keys():
    ann = {
        "currency_code": "EUR",
        "subtotal": "1.234,56",
        "items": [item("X", "20,00", adjustments=[
            {"name": "d", "translated_name": "d", "amount": "-1,20", "is_new_price": False},
        ])],
        "charges": [charge("VAT", "3,80")],
    }
    _normalize_amounts_in_annotation(ann)
    assert ann["subtotal"] == "1234.56"
    assert ann["items"][0]["total"] == "20.00"
    assert ann["items"][0]["adjustments"][0]["amount"] == "-1.20"
    assert ann["charges"][0]["amount"] == "3.80"


# --- ledger gap helpers -----------------------------------------------------

def test_ledger_gap_counts_non_included_charges():
    ann = {
        "currency_code": "USD",
        "items_total": "10.00",
        "charges": [charge("Tax", "0.80"), charge("VAT info", "9.99", included=True)],
        "receipt_total": "10.80",
    }
    assert _items_receipt_gap(ann) == 0.0
    assert _is_reconciled(ann)


# --- standard_post_process end-to-end ---------------------------------------

def test_standard_post_process_v2_annotation():
    ann = {
        "receipt_language": "English",
        "currency_code": "USD",
        "items": [
            item("Burger", "10.00"),
            item("Fries", "4.00", adjustments=[
                {"name": "promo", "translated_name": "promo", "amount": "-1.00", "is_new_price": False},
            ]),
        ],
        "subtotal": "13.00",
        "items_total": "14.00",
        "charges": [
            charge("Sales tax", "1.04"),
            charge("Rounding", "0.00"),
        ],
        "receipt_total": "14.04",
    }
    metrics = standard_post_process(ann, "USD")
    assert ann["items"][1]["total"] == "3.00"
    assert ann["items_total"] == 13.0
    assert ann["ai_items_total"] == "14.00"
    assert ann["totals_reconciled"] is True
    assert len(ann["charges"]) == 1  # zero-amount rounding row dropped
    assert metrics["item_adjustments_folded"] == 1
    assert metrics["items_match_receipt_total"] is True
    assert metrics["ledger_gap"] == 0.0
    assert metrics["subtotal_matches_items"] is True
    assert metrics["has_tax"] is True
    assert metrics["has_tip"] is False
    assert metrics["charges_count"] == 1
    assert metrics["reconciliation_action"] == "none"
    assert ann["items"][0]["category"] == "item"


def test_standard_post_process_synthesizes_from_net_total():
    ann = {
        "receipt_language": "English",
        "currency_code": "GBP",
        "items": [],
        "charges": [charge("Booking fee", "1.50")],
        "receipt_total": "21.50",
        "receipt_establishment_name": "Car Park",
    }
    metrics = standard_post_process(ann, "GBP")
    assert metrics["synthesized_total_only_item"] is True
    assert ann["items"][0]["total"] == "20.00"
    assert ann["totals_reconciled"] is True
