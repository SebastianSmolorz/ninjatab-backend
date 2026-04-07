from dataclasses import dataclass
from typing import List
from ninjatab.currencies.exchange import convert_amount, ExchangeRateNotFoundError


@dataclass
class Balance:
    """Represents a person's net balance in minor currency units"""
    person_id: int
    balance: int


@dataclass
class Transaction:
    """Represents a simplified settlement transaction in minor currency units"""
    payer_id: int
    payee_id: int
    amount: int


def calculate_tab_balances(tab, settlement_currency: str = None) -> List[Balance]:
    """
    Calculate net balances for all people in a tab based on bills.
    Converts all amounts to settlement_currency if provided.
    Excludes archived bills.

    Args:
        tab: Tab instance
        settlement_currency: Currency to convert all balances to. If None, uses original currencies.

    Returns:
        List of Balance objects representing net balances for each person

    Raises:
        ExchangeRateNotFoundError: If currency conversion is needed but no rate is found
    """
    balances_by_person = {}

    # Filter out archived bills in Python to preserve prefetch_related
    bills = [b for b in tab.bills.all() if b.status != 'archived']

    for bill in bills:
        if not bill.paid_by:
            continue

        payer_id = bill.paid_by.id
        payer_total = 0
        bill_currency = bill.currency

        # Process each line item in the bill
        for line_item in bill.line_items.all():
            # Get all claims for this line item
            claims = line_item.person_claims.all()

            for claim in claims:
                person_id = claim.person.id
                amount = claim.calculated_amount or 0

                if amount <= 0:
                    continue

                # Skip if the person claiming is the payer
                if person_id == payer_id:
                    continue

                # Convert to settlement currency if specified
                if settlement_currency and bill_currency != settlement_currency:
                    amount = convert_amount(amount, bill_currency, settlement_currency)

                # Debtor owes money (negative balance)
                balances_by_person[person_id] = balances_by_person.get(person_id, 0) - amount
                payer_total += amount

        # Payer is owed money (positive balance)
        balances_by_person[payer_id] = balances_by_person.get(payer_id, 0) + payer_total

    # Convert to Balance objects, filtering out zero balances
    return [Balance(person_id=pid, balance=bal) for pid, bal in balances_by_person.items() if bal != 0]


def simp(balances: List[Balance]) -> List[Transaction]:
    """
    Simplify balances into minimal transactions using greedy algorithm.
    Repeatedly pairs largest creditor with largest debtor.
    """
    # Create mutable dict of balances, excluding zeros
    bals = {b.person_id: b.balance for b in balances if b.balance != 0}

    if not bals:
        return []

    transactions = []

    def max_creditor():
        """Find person owed the most money"""
        return max(bals, key=bals.get)

    def max_debtor():
        """Find person who owes the most money"""
        return min(bals, key=bals.get)

    # Continue until all balances are effectively zero
    while bals:
        creditor_id = max_creditor()
        debtor_id = max_debtor()

        # Stop if remaining balances are negligible (less than 1 minor unit)
        if abs(bals[creditor_id]) < 1 and abs(bals[debtor_id]) < 1:
            break

        # Calculate settlement amount (min of what's owed and what's due)
        amount = min(bals[creditor_id], -bals[debtor_id])

        # Update balances
        bals[creditor_id] -= amount
        bals[debtor_id] += amount

        # Remove zeros to avoid processing them
        if abs(bals[creditor_id]) < 1:
            del bals[creditor_id]
        if abs(bals[debtor_id]) < 1:
            del bals[debtor_id]

        # Record transaction
        transactions.append(Transaction(payer_id=debtor_id, payee_id=creditor_id, amount=amount))

    return transactions


def simp_tab(tab, settlement_currency: str = None) -> List[Transaction]:
    """
    Calculate and simplify all balances for a tab.
    Returns list of minimal settlement transactions.

    Args:
        tab: Tab instance
        settlement_currency: Currency to use for settlements. If None, uses original currencies.

    Returns:
        List of Transaction objects representing simplified settlements

    Raises:
        ExchangeRateNotFoundError: If currency conversion is needed but no rate is found
    """
    balances = calculate_tab_balances(tab, settlement_currency)
    transactions = simp(balances)
    return transactions
