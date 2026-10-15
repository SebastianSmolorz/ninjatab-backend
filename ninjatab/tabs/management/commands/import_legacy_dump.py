"""
One-off: import a pre-int-currency dumpdata file (e.g. ~/Desktop/tabs.json).

    python manage.py import_legacy_dump tabs.json --tab 6 [--owner <user_id>] [--dry-run]

- Money in the dump is decimal major units ("430.91"); stored as minor units.
- Rows get new PKs (old -> new mapped), so nothing existing is overwritten.
- No users are linked to TabPerson. Tab.created_by is --owner, else None.
- The dump's exchange rates are ignored; settlement_amount uses the current
  rates in currencies.ExchangeRate, same as the API.
"""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ninjatab.currencies.currency_utils import decimal_to_minor
from ninjatab.currencies.exchange import convert_amount
from ninjatab.tabs.models import (
    Bill, BillStatus, LineItem, PersonLineItemClaim, Settlement, SplitType, Tab, TabPerson,
)


class DryRun(Exception):
    pass


def _create(model, f, **kwargs):
    obj = model.objects.create(**kwargs)
    # auto_now/auto_now_add ignore passed values, so restore the originals after insert
    model.objects.filter(pk=obj.pk).update(created_at=f["created_at"], updated_at=f["updated_at"])
    return obj


class Command(BaseCommand):
    help = "One-off import of a legacy (decimal-money) dumpdata JSON file"

    def add_arguments(self, parser):
        parser.add_argument("json_file")
        parser.add_argument("--tab", type=int, action="append", help="Dump tab pk to import (repeatable; default all)")
        parser.add_argument("--owner", type=int, help="User id to set as Tab.created_by")
        parser.add_argument("--dry-run", action="store_true", help="Run the import, then roll back")

    def handle(self, *args, json_file, tab, owner, dry_run, **options):
        with open(json_file) as fh:
            data = json.load(fh)
        rows = {}
        for o in data:
            rows.setdefault(o["model"], []).append(o)

        owner_user = get_user_model().objects.get(pk=owner) if owner else None
        tab_pks = set(tab) if tab else {o["pk"] for o in rows.get("tabs.tab", [])}
        if not tab_pks <= {o["pk"] for o in rows.get("tabs.tab", [])}:
            raise CommandError(f"Tab pk(s) not in dump: {tab_pks}")

        try:
            with transaction.atomic():
                self._import(rows, tab_pks, owner_user)
                if dry_run:
                    raise DryRun
        except DryRun:
            self.stdout.write(self.style.WARNING("Dry run: rolled back."))

    def _import(self, rows, tab_pks, owner_user):
        tabs, people, bills, items = {}, {}, {}, {}

        for o in rows["tabs.tab"]:
            if o["pk"] not in tab_pks:
                continue
            f = o["fields"]
            tabs[o["pk"]] = _create(
                Tab, f,
                name=f["name"],
                description=f["description"],
                created_by=owner_user,
                default_currency=f["default_currency"],
                settlement_currency=f.get("settlement_currency", f["default_currency"]),
            )

        for o in rows["tabs.tabperson"]:
            f = o["fields"]
            if f["tab"] in tabs:
                people[o["pk"]] = _create(TabPerson, f, tab=tabs[f["tab"]], name=f["name"])

        for o in rows["tabs.bill"]:
            f = o["fields"]
            if f["tab"] in tabs:
                bills[o["pk"]] = _create(
                    Bill, f,
                    tab=tabs[f["tab"]],
                    description=f["description"],
                    currency=f["currency"],
                    status=f["status"],
                    creator=people[f["creator"]],
                    paid_by=people[f["paid_by"]],
                    date=f["date"],
                )

        for o in rows["tabs.lineitem"]:
            f = o["fields"]
            if f["bill"] in bills:
                bill = bills[f["bill"]]
                items[o["pk"]] = _create(
                    LineItem, f,
                    bill=bill,
                    description=f["description"],
                    value=decimal_to_minor(Decimal(f["value"]), bill.currency),
                    split_type=f["split_type"],
                )

        claim_count = 0
        for o in rows["tabs.personlineitemclaim"]:
            f = o["fields"]
            if f["line_item"] not in items:
                continue
            item = items[f["line_item"]]
            cur = item.bill.currency
            sv = f["split_value"]
            if sv is not None:
                # shares are whole counts; VALUE splits are money -> minor units
                sv = int(Decimal(sv)) if item.split_type == SplitType.SHARES else decimal_to_minor(Decimal(sv), cur)
            calc = decimal_to_minor(Decimal(f["calculated_amount"]), cur) if f["calculated_amount"] is not None else None
            _create(
                PersonLineItemClaim, f,
                person=people[f["person"]],
                line_item=item,
                split_value=sv,
                calculated_amount=calc,
                settlement_amount=None if calc is None else convert_amount(calc, cur, item.bill.tab.settlement_currency),
                has_claimed=f["has_claimed"],
            )
            claim_count += 1

        settlement_count = 0
        for o in rows.get("tabs.settlement", []):
            f = o["fields"]
            if f["tab"] in tabs:
                _create(
                    Settlement, f,
                    tab=tabs[f["tab"]],
                    from_person=people[f["from_person"]],
                    to_person=people[f["to_person"]],
                    amount=decimal_to_minor(Decimal(f["amount"]), f["currency"]),
                    currency=f["currency"],
                    paid=f["paid"],
                )
                settlement_count += 1

        # Settled tabs: mirror _close_tab's snapshot (api/tabs.py), stamped with the dump's updated_at
        settled = {o["pk"]: o["fields"]["updated_at"] for o in rows["tabs.tab"] if o["pk"] in tabs and o["fields"]["is_settled"]}
        for old_pk, settled_at in settled.items():
            t = tabs[old_pk]
            total = sum(
                convert_amount(sum(li.value for li in b.line_items.all()), b.currency, t.settlement_currency)
                for b in t.bills.exclude(status=BillStatus.ARCHIVED).prefetch_related("line_items")
            )
            Tab.objects.filter(pk=t.pk).update(is_settled=True, settled_at=settled_at, settlement_currency_settled_total=total)

        for old_pk, t in tabs.items():
            self.stdout.write(f"  dump tab {old_pk} -> Tab {t.pk} {t.uuid} '{t.name}'")
        self.stdout.write(self.style.SUCCESS(
            f"Imported {len(tabs)} tabs, {len(people)} people, {len(bills)} bills, "
            f"{len(items)} line items, {claim_count} claims, {settlement_count} settlements"
        ))
