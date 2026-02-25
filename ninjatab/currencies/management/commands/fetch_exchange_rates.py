import requests
from decimal import Decimal, ROUND_HALF_UP
from itertools import permutations

from django.core.management.base import BaseCommand
from django.conf import settings
from datetime import datetime, timezone

from ninjatab.currencies.models import Currency, ExchangeRate


class Command(BaseCommand):
    help = "Fetch exchange rates from Open Exchange Rates API and create rates for all currency pairs"

    def handle(self, *args, **options):
        app_id = getattr(settings, 'OPEN_EXCHANGE_RATES_APP_ID', '') or ''
        if not app_id:
            self.stderr.write(self.style.ERROR(
                "OPEN_EXCHANGE_RATES_APP_ID not set in settings/environment"
            ))
            return

        supported_codes = [c.value for c in Currency]
        symbols = ','.join(supported_codes)
        url = f"https://openexchangerates.org/api/latest.json?app_id={app_id}&symbols={symbols}"

        self.stdout.write(f"Fetching rates from Open Exchange Rates (base=USD)...")

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            self.stderr.write(self.style.ERROR(f"Failed to fetch rates: {e}"))
            return

        usd_rates = data.get('rates', {})
        timestamp = data.get('timestamp')
        effective_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)

        # Filter to only currencies we support
        available = {code: Decimal(str(usd_rates[code])) for code in supported_codes if code in usd_rates}
        # USD is always 1 (base)
        available['USD'] = Decimal('1')

        missing = set(supported_codes) - set(available.keys())
        if missing:
            self.stdout.write(self.style.WARNING(f"Missing rates for: {', '.join(missing)}"))

        self.stdout.write(f"Got rates for {len(available)} currencies, effective {effective_date}")

        # Calculate cross rates for all pairs and bulk create
        rates_to_create = []
        for from_code, to_code in permutations(available.keys(), 2):
            # rate = how many to_currency per 1 from_currency
            # from_code -> USD -> to_code
            # 1 from_code = (1 / usd_rate_from) USD = (usd_rate_to / usd_rate_from) to_code
            cross_rate = (available[to_code] / available[from_code]).quantize(
                Decimal('0.000001'), rounding=ROUND_HALF_UP
            )
            rates_to_create.append(ExchangeRate(
                from_currency=from_code,
                to_currency=to_code,
                rate=cross_rate,
                effective_date=effective_date,
            ))

        ExchangeRate.objects.bulk_create(
            rates_to_create,
            update_conflicts=True,
            unique_fields=['from_currency', 'to_currency', 'effective_date'],
            update_fields=['rate'],
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created/updated {len(rates_to_create)} exchange rate pairs"
        ))
