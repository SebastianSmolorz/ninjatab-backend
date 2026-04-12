import pytest
from decimal import Decimal
from ninjatab.tabs.simp import Balance, Transaction, simp, calculate_tab_balances, simp_tab
from .factories import BillFactory, LineItemFactory, PersonLineItemClaimFactory, ExchangeRateFactory


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def assert_settlements_net_to_zero(balances, transactions):
    """After applying all transactions, every person's net should be zero."""
    nets = {b.person_id: b.balance for b in balances}
    for t in transactions:
        nets[t.payer_id] = nets.get(t.payer_id, 0) + t.amount
        nets[t.payee_id] = nets.get(t.payee_id, 0) - t.amount
    for pid, val in nets.items():
        assert abs(val) < 1, f"Person {pid} has residual balance {val}"


def _balances_dict(balances):
    """Convert list of Balance to {person_id: balance} dict."""
    return {b.person_id: b.balance for b in balances}


def _add_claim(line_item, person, amount):
    """Shorthand to create a claim on a line item."""
    return PersonLineItemClaimFactory(
        person=person, line_item=line_item, calculated_amount=amount,
    )


def _prefetch_tab(tab):
    """Re-fetch tab with the prefetches that simp expects."""
    from ninjatab.tabs.models import Tab
    return Tab.objects.prefetch_related(
        "bills__line_items__person_claims__person",
        "bills__paid_by",
    ).get(pk=tab.pk)


# ===========================================================================
# A. Pure simp() algorithm tests — no database
# ===========================================================================

class TestSimp:
    def test_empty(self):
        assert simp([]) == []

    def test_two_people(self):
        balances = [Balance(1, -1000), Balance(2, 1000)]
        txns = simp(balances)
        assert len(txns) == 1
        assert txns[0] == Transaction(payer_id=1, payee_id=2, amount=1000)
        assert_settlements_net_to_zero(balances, txns)

    def test_three_people_one_creditor(self):
        balances = [Balance(1, -600), Balance(2, -400), Balance(3, 1000)]
        txns = simp(balances)
        assert len(txns) == 2
        assert_settlements_net_to_zero(balances, txns)

    def test_one_payer_many_debtors(self):
        balances = [
            Balance(1, 3000),
            Balance(2, -1000),
            Balance(3, -1000),
            Balance(4, -1000),
        ]
        txns = simp(balances)
        assert len(txns) == 3
        assert all(t.payee_id == 1 for t in txns)
        assert_settlements_net_to_zero(balances, txns)

    def test_already_settled(self):
        balances = [Balance(1, 0), Balance(2, 0)]
        assert simp(balances) == []

    def test_circular_three_way(self):
        # A owes B 100, B owes C 100, C owes A 100 → nets to zero
        balances = [Balance(1, 0), Balance(2, 0), Balance(3, 0)]
        assert simp(balances) == []

        # Non-trivial circular: A=-300, B=+100, C=+200
        balances = [Balance(1, -300), Balance(2, 100), Balance(3, 200)]
        txns = simp(balances)
        assert len(txns) <= 2
        assert_settlements_net_to_zero(balances, txns)


# ===========================================================================
# B. calculate_tab_balances() tests — need DB
# ===========================================================================

@pytest.mark.django_db
class TestCalculateTabBalances:
    def test_simple_two_person_split(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li = LineItemFactory(bill=bill, value=1000)
        _add_claim(li, p["Alice"], 500)  # self-claim, should be skipped
        _add_claim(li, p["Bob"], 500)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        bd = _balances_dict(balances)
        assert bd[p["Alice"].id] == 500
        assert bd[p["Bob"].id] == -500

    def test_archived_bills_skipped(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"], status="archived")
        li = LineItemFactory(bill=bill, value=1000)
        _add_claim(li, p["Bob"], 500)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        assert balances == []

    def test_no_payer_skipped(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])
        bill = BillFactory(tab=tab, paid_by=None, creator=p["Alice"])
        li = LineItemFactory(bill=bill, value=1000)
        _add_claim(li, p["Bob"], 500)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        assert balances == []

    def test_self_claims_only(self, tab_with_people):
        tab, p = tab_with_people(["Alice"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li = LineItemFactory(bill=bill, value=1000)
        _add_claim(li, p["Alice"], 1000)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        assert balances == []

    def test_multiple_bills(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])

        bill1 = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li1 = LineItemFactory(bill=bill1, value=300)
        _add_claim(li1, p["Bob"], 300)

        bill2 = BillFactory(tab=tab, paid_by=p["Bob"], creator=p["Bob"])
        li2 = LineItemFactory(bill=bill2, value=200)
        _add_claim(li2, p["Alice"], 200)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        bd = _balances_dict(balances)
        assert bd[p["Alice"].id] == 100   # +300 - 200
        assert bd[p["Bob"].id] == -100    # -300 + 200

    def test_multiple_line_items(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li1 = LineItemFactory(bill=bill, value=400)
        li2 = LineItemFactory(bill=bill, value=600)
        _add_claim(li1, p["Bob"], 400)
        _add_claim(li2, p["Bob"], 600)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        bd = _balances_dict(balances)
        assert bd[p["Alice"].id] == 1000
        assert bd[p["Bob"].id] == -1000

    def test_indivisible_split_payer_absorbs_rounding(self, tab_with_people):
        """£10.00 (1000p) split equally 3 ways cannot divide evenly.

        Upstream (_create_person_claims) rounds per-claim: round(1000/3) = 333 each.
        Total claims = 999, which is 1p short of the 1000p bill.

        calculate_tab_balances only sums non-self claims as payer_total, so the
        payer implicitly absorbs the rounding remainder. Balances always net to
        exactly zero — simp() never has to deal with a residual.

        Alice (payer): credited payer_total = 333 + 333 = 666
        Bob:  -333
        Charlie: -333
        Net: 666 - 333 - 333 = 0

        Alice paid 1000 but is only reimbursed 666, absorbing 334 (her 333
        share + the 1p rounding loss).
        """
        tab, p = tab_with_people(["Alice", "Bob", "Charlie"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li = LineItemFactory(bill=bill, value=1000)
        # Simulate round(1000 * 1/3) = 333 for each person
        _add_claim(li, p["Alice"], 333)
        _add_claim(li, p["Bob"], 333)
        _add_claim(li, p["Charlie"], 333)

        balances = calculate_tab_balances(_prefetch_tab(tab))
        bd = _balances_dict(balances)

        assert bd[p["Alice"].id] == 666    # payer credited sum of others' claims
        assert bd[p["Bob"].id] == -333
        assert bd[p["Charlie"].id] == -333
        assert sum(bd.values()) == 0       # balances always net to zero

        # simp settles cleanly — no residual penny issues
        txns = simp(balances)
        assert len(txns) == 2
        assert_settlements_net_to_zero(balances, txns)

    def test_currency_conversion(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"], currency="GBP")
        ExchangeRateFactory(from_currency="USD", to_currency="GBP", rate=Decimal("0.80"))

        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"], currency="USD")
        li = LineItemFactory(bill=bill, value=1000)
        _add_claim(li, p["Bob"], 1000)

        balances = calculate_tab_balances(_prefetch_tab(tab), settlement_currency="GBP")
        bd = _balances_dict(balances)
        assert bd[p["Alice"].id] == 800   # 1000 USD cents * 0.80
        assert bd[p["Bob"].id] == -800


# ===========================================================================
# C. End-to-end simp_tab() black-box tests
# ===========================================================================

@pytest.mark.django_db
class TestSimpTabEndToEnd:
    def test_two_person(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li = LineItemFactory(bill=bill, value=1000)
        _add_claim(li, p["Alice"], 500)
        _add_claim(li, p["Bob"], 500)

        txns = simp_tab(_prefetch_tab(tab), "GBP")
        assert len(txns) == 1
        assert txns[0].payer_id == p["Bob"].id
        assert txns[0].payee_id == p["Alice"].id
        assert txns[0].amount == 500

    def test_three_person_dinner(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob", "Charlie"])
        bill = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li = LineItemFactory(bill=bill, value=3000)
        _add_claim(li, p["Alice"], 1000)
        _add_claim(li, p["Bob"], 1000)
        _add_claim(li, p["Charlie"], 1000)

        txns = simp_tab(_prefetch_tab(tab), "GBP")
        assert len(txns) == 2
        assert all(t.payee_id == p["Alice"].id for t in txns)

        paid_amounts = {t.payer_id: t.amount for t in txns}
        assert paid_amounts[p["Bob"].id] == 1000
        assert paid_amounts[p["Charlie"].id] == 1000

    def test_multiple_payers(self, tab_with_people):
        """Alice pays bill1, Bob pays bill2 — verify net settlements."""
        tab, p = tab_with_people(["Alice", "Bob", "Charlie"])

        # Bill 1: Alice pays 2000 (Bob claims 1000, Charlie claims 1000)
        bill1 = BillFactory(tab=tab, paid_by=p["Alice"], creator=p["Alice"])
        li1 = LineItemFactory(bill=bill1, value=2000)
        _add_claim(li1, p["Bob"], 1000)
        _add_claim(li1, p["Charlie"], 1000)

        # Bill 2: Bob pays 600 (Alice claims 300, Charlie claims 300)
        bill2 = BillFactory(tab=tab, paid_by=p["Bob"], creator=p["Bob"])
        li2 = LineItemFactory(bill=bill2, value=600)
        _add_claim(li2, p["Alice"], 300)
        _add_claim(li2, p["Charlie"], 300)

        # Net: Alice=+2000-300=+1700, Bob=-1000+600=−400, Charlie=-1000-300=−1300
        tab_fresh = _prefetch_tab(tab)
        balances = calculate_tab_balances(tab_fresh)
        txns = simp(balances)

        assert len(txns) == 2
        assert_settlements_net_to_zero(balances, txns)

        # Verify exact net balances
        bd = _balances_dict(balances)
        assert bd[p["Alice"].id] == 1700
        assert bd[p["Bob"].id] == -400
        assert bd[p["Charlie"].id] == -1300

    def test_complex_six_bills_four_people(self, tab_with_people):
        """Realistic group trip: 4 people, 6 expenses, different payers.

        Bill 1: Dinner  — Alice pays £60, split 4 ways (1500 each)
        Bill 2: Taxi    — Bob pays £20 (Alice 800, Charlie 600, Diana 600)
        Bill 3: Groceries — Charlie pays £35 (Alice 1200, Bob 1000, Diana 1300)
        Bill 4: Cinema  — Diana pays £48, split 4 ways (1200 each)
        Bill 5: Drinks  — Alice pays £24 (Bob 900, Charlie 700, Diana 800)
        Bill 6: Brunch  — Bob pays £32 (Alice 800, Charlie 1000, Diana 600)

        Expected net balances:
          Alice:   +4500 +2400 -800 -1200 -1200 -800  = +2900
          Bob:     +2000 +2400 -1500 -1000 -1200 -900 = -200
          Charlie: +3500 -1500 -600 -1200 -700 -1000  = -1500
          Diana:   +3600 -1500 -600 -1300 -800 -600   = -1200
        """
        tab, p = tab_with_people(["Alice", "Bob", "Charlie", "Diana"])

        def bill(payer, claims):
            b = BillFactory(tab=tab, paid_by=p[payer], creator=p[payer])
            total = sum(v for _, v in claims)
            li = LineItemFactory(bill=b, value=total)
            for name, amount in claims:
                _add_claim(li, p[name], amount)

        bill("Alice",   [("Alice", 1500), ("Bob", 1500), ("Charlie", 1500), ("Diana", 1500)])
        bill("Bob",     [("Bob", 0),      ("Alice", 800), ("Charlie", 600),  ("Diana", 600)])
        bill("Charlie", [("Charlie", 0),  ("Alice", 1200), ("Bob", 1000),   ("Diana", 1300)])
        bill("Diana",   [("Diana", 1200), ("Alice", 1200), ("Bob", 1200),   ("Charlie", 1200)])
        bill("Alice",   [("Alice", 0),    ("Bob", 900),   ("Charlie", 700),  ("Diana", 800)])
        bill("Bob",     [("Bob", 800),    ("Alice", 800),  ("Charlie", 1000), ("Diana", 600)])

        tab_fresh = _prefetch_tab(tab)
        balances = calculate_tab_balances(tab_fresh)
        bd = _balances_dict(balances)

        assert bd[p["Alice"].id] == 2900
        assert bd[p["Bob"].id] == -200
        assert bd[p["Charlie"].id] == -1500
        assert bd[p["Diana"].id] == -1200

        txns = simp(balances)

        # Greedy: 3 transactions, all paying Alice
        assert len(txns) == 3
        assert all(t.payee_id == p["Alice"].id for t in txns)
        total_paid = sum(t.amount for t in txns)
        assert total_paid == 2900
        assert_settlements_net_to_zero(balances, txns)

    def test_no_bills(self, tab_with_people):
        tab, p = tab_with_people(["Alice", "Bob"])
        txns = simp_tab(_prefetch_tab(tab), "GBP")
        assert txns == []
