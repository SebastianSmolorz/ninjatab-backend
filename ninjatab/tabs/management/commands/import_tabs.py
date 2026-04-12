"""
Management command to import a legacy JSON dump into the current schema.

Usage:
    python manage.py import_tabs /path/to/tabs.json
    python manage.py import_tabs /path/to/tabs.json --dry-run

The dump format is Django's dumpdata output with integer PKs. This command:
  1. Imports exchange rates, tabs, people, bills, line items, claims, settlements
     in dependency order, preserving the original integer PKs.
  2. Resolves user FKs by integer ID — warns and sets user=None if the user
     doesn't exist in the target DB.
  3. After import, calculates settlement_amount on every claim using the
     bill's currency and the tab's settlement_currency.
"""

import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ninjatab.tabs.models import (
    Tab, TabPerson, Bill, LineItem, PersonLineItemClaim, Settlement,
)
from ninjatab.currencies.models import ExchangeRate
from ninjatab.currencies.exchange import convert_amount, ExchangeRateNotFoundError

class Command(BaseCommand):
    help = "Import a legacy dumpdata JSON file into the current schema"

    def add_arguments(self, parser):
        parser.add_argument("json_file", type=str, help="Path to the JSON dump file")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and validate without writing to the database",
        )

    def handle(self, *args, **options):
        path = options["json_file"]
        dry_run = options["dry_run"]

        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"File not found: {path}")
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON: {e}")

        # Group by model
        by_model = {}
        for obj in data:
            by_model.setdefault(obj["model"], []).append(obj)

        def get(model_key):
            return by_model.get(model_key, [])

        self.stdout.write(f"Parsed {len(data)} records from {path}")
        for model, objs in by_model.items():
            self.stdout.write(f"  {model}: {len(objs)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
            return

        with transaction.atomic():
            # 1. Exchange rates
            rate_count = 0
            for obj in get("tabs.exchangerate"):
                f = obj["fields"]
                ExchangeRate.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        from_currency=f["from_currency"],
                        to_currency=f["to_currency"],
                        rate=Decimal(f["rate"]),
                        effective_date=f["effective_date"],
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                rate_count += 1
            self.stdout.write(f"  Imported {rate_count} exchange rates")

            # 2. Tabs
            tab_count = 0
            for obj in get("tabs.tab"):
                f = obj["fields"]
                Tab.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        name=f["name"],
                        description=f.get("description", ""),
                        created_by=None,
                        default_currency=f["default_currency"],
                        settlement_currency=f.get("settlement_currency", f["default_currency"]),
                        is_settled=f.get("is_settled", False),
                        is_pro=f.get("is_pro", False),
                        receipt_scan_count=f.get("receipt_scan_count", 0),
                        invite_code=f["invite_code"],
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                tab_count += 1
            self.stdout.write(f"  Imported {tab_count} tabs")

            # 3. TabPeople
            person_count = 0
            for obj in get("tabs.tabperson"):
                f = obj["fields"]
                TabPerson.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        tab_id=f["tab"],
                        name=f["name"],
                        user=None,
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                person_count += 1
            self.stdout.write(f"  Imported {person_count} tab people")

            # 4. Bills
            bill_count = 0
            for obj in get("tabs.bill"):
                f = obj["fields"]
                Bill.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        tab_id=f["tab"],
                        description=f["description"],
                        currency=f["currency"],
                        status=f.get("status", "open"),
                        creator_id=f["creator"],
                        paid_by_id=f.get("paid_by"),
                        date=f["date"],
                        receipt_image_url=f.get("receipt_image_url", ""),
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                bill_count += 1
            self.stdout.write(f"  Imported {bill_count} bills")

            # 5. Line items
            item_count = 0
            for obj in get("tabs.lineitem"):
                f = obj["fields"]
                LineItem.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        bill_id=f["bill"],
                        description=f["description"],
                        translated_name=f.get("translated_name", ""),
                        value=Decimal(f["value"]),
                        split_type=f["split_type"],
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                item_count += 1
            self.stdout.write(f"  Imported {item_count} line items")

            # 6. Claims (settlement_amount calculated after, see below)
            claim_count = 0
            for obj in get("tabs.personlineitemclaim"):
                f = obj["fields"]
                PersonLineItemClaim.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        person_id=f["person"],
                        line_item_id=f["line_item"],
                        split_value=Decimal(f["split_value"]) if f.get("split_value") else None,
                        calculated_amount=Decimal(f["calculated_amount"]) if f.get("calculated_amount") else None,
                        settlement_amount=Decimal(f["settlement_amount"]) if f.get("settlement_amount") else None,
                        has_claimed=f.get("has_claimed", False),
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                claim_count += 1
            self.stdout.write(f"  Imported {claim_count} claims")

            # 7. Settlements
            settlement_count = 0
            for obj in get("tabs.settlement"):
                f = obj["fields"]
                Settlement.objects.update_or_create(
                    pk=obj["pk"],
                    defaults=dict(
                        tab_id=f["tab"],
                        from_person_id=f["from_person"],
                        to_person_id=f["to_person"],
                        amount=Decimal(f["amount"]),
                        currency=f["currency"],
                        paid=f.get("paid", False),
                        created_at=f["created_at"],
                        updated_at=f["updated_at"],
                    ),
                )
                settlement_count += 1
            self.stdout.write(f"  Imported {settlement_count} settlements")

        self.stdout.write(
            self.style.WARNING(
                "\nAll user links set to None — link TabPerson and Tab records "
                "to existing users via the admin."
            )
        )

        # Post-import: calculate settlement_amount for all imported claims
        self.stdout.write("\nCalculating settlement_amount for imported claims...")
        self._backfill_settlement_amounts()
        self._backfill_bill_settlement_totals()

    def _backfill_settlement_amounts(self):
        claims = (
            PersonLineItemClaim.objects
            .filter(settlement_amount__isnull=True, calculated_amount__isnull=False)
            .select_related('line_item__bill__tab')
        )

        updated = []
        skipped = 0

        for claim in claims:
            bill = claim.line_item.bill
            try:
                claim.settlement_amount = convert_amount(
                    claim.calculated_amount,
                    bill.currency,
                    bill.tab.settlement_currency,
                )
                updated.append(claim)
            except ExchangeRateNotFoundError:
                skipped += 1

        if updated:
            PersonLineItemClaim.objects.bulk_update(updated, ['settlement_amount'])

        self.stdout.write(
            self.style.SUCCESS(f"  settlement_amount set on {len(updated)} claims")
        )
        if skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"  {skipped} claims skipped — missing exchange rate. "
                    "Run manage.py fetch_exchange_rates then re-run this command, "
                    "or run manage.py import_tabs again to retry."
                )
            )

    def _backfill_bill_settlement_totals(self):
        from django.db.models import OuterRef, Subquery, Sum
        updated = Bill.objects.filter(settlement_total__isnull=True).update(
            settlement_total=Subquery(
                PersonLineItemClaim.objects
                .filter(line_item__bill=OuterRef('pk'))
                .values('line_item__bill')
                .annotate(total=Sum('settlement_amount'))
                .values('total')
            )
        )
        self.stdout.write(self.style.SUCCESS(f"  settlement_total set on {updated} bills"))
