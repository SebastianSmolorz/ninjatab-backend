import urllib.request
import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone

from ninjatab.tabs.models import Currency, ExchangeRate


class Command(BaseCommand):
    help = "Fetch latest exchange rates from Open Exchange Rates and store them"

    def handle(self, *args, **options):
        app_id = settings.OPEN_EXCHANGE_RATES_APP_ID
        if not app_id:
            raise CommandError(
                "OPEN_EXCHANGE_RATES_APP_ID is not set. "
                "Add it to your .env file or environment variables."
            )

        currency_codes = [c.value for c in Currency]
        symbols = ",".join(currency_codes)
        url = (
            f"https://openexchangerates.org/api/latest.json"
            f"?app_id={app_id}&symbols={symbols}"
        )

        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            raise CommandError(f"Failed to fetch rates: {e}")

        rates = data.get("rates", {})
        # todo cannot change base so will have to go usd -> gbp -> others
        # todo need to ensure base currency requested is GBP
        # todo once all fetched to gbp, convert common bases like EUR, USD, PLN
        # todo rest of bases should be lazy. Only request these when a tab is open with a different base
        base = data.get("base", "USD")
        now = timezone.now()
        created = 0

        for code in currency_codes:
            if code == base:
                continue
            rate_value = rates.get(code)
            if rate_value is None:
                self.stderr.write(f"No rate returned for {code}, skipping")
                continue

            _, was_created = ExchangeRate.objects.update_or_create(
                from_currency=base,
                to_currency=code,
                effective_date=now,
                defaults={"rate": Decimal(str(rate_value))},
            )
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Updated {len(currency_codes) - 1} rates (base {base}), {created} new records"
        ))
