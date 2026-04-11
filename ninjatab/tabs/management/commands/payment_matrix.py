from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from ninjatab.tabs.models import Tab
from ninjatab.currencies.exchange import convert_amount, ExchangeRateNotFoundError


class Command(BaseCommand):
    help = "Show a payment matrix for a tab: who paid how much for whom"

    def add_arguments(self, parser):
        parser.add_argument("tab_id", type=int)

    def handle(self, *args, **options):
        tab_id = options["tab_id"]

        try:
            tab = Tab.objects.prefetch_related(
                "people",
                "bills__paid_by",
                "bills__line_items__person_claims__person",
            ).get(id=tab_id)
        except Tab.DoesNotExist:
            raise CommandError(f"Tab {tab_id} does not exist")

        settlement_currency = tab.settlement_currency
        people_by_id = {p.id: p for p in tab.people.all()}

        # matrix[payer_id][claimant_id] = total amount paid for them
        matrix = defaultdict(lambda: defaultdict(Decimal))

        bills = tab.bills.exclude(status="archived")

        for bill in bills:
            if not bill.paid_by:
                continue

            payer_id = bill.paid_by.id
            bill_currency = bill.currency

            for line_item in bill.line_items.all():
                for claim in line_item.person_claims.all():
                    amount = claim.calculated_amount or Decimal("0")
                    if amount <= 0:
                        continue

                    claimant_id = claim.person.id

                    try:
                        converted = convert_amount(
                            amount, bill_currency, settlement_currency
                        )
                    except ExchangeRateNotFoundError as e:
                        raise CommandError(str(e))

                    matrix[payer_id][claimant_id] += converted

        # Print results
        self.stdout.write(
            f'\nTab: "{tab.name}" (Settlement currency: {settlement_currency})'
        )
        self.stdout.write("\u2500" * 38)

        for payer_id, claims in matrix.items():
            payer = people_by_id.get(payer_id)
            if not payer:
                continue

            self.stdout.write(f"{payer.name} paid for:")
            total = Decimal("0")

            for claimant_id, amount in claims.items():
                claimant = people_by_id.get(claimant_id)
                if not claimant:
                    continue

                suffix = " (self)" if claimant_id == payer_id else ""
                self.stdout.write(
                    f"  {claimant.name}{suffix}: {amount:.2f} {settlement_currency}"
                )
                total += amount

            self.stdout.write(
                f"  Total paid: {total:.2f} {settlement_currency}\n"
            )

        self.stdout.write("\u2500" * 38)
        self.stdout.write("Summary - each person's self-payment:")

        for person_id, person in people_by_id.items():
            self_amount = matrix.get(person_id, {}).get(person_id, Decimal("0"))
            self.stdout.write(
                f"  {person.name}: {self_amount:.2f} {settlement_currency}"
            )

        self.stdout.write("")
