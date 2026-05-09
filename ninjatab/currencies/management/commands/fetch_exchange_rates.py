import requests
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.conf import settings
from datetime import datetime, timezone

from ninjatab.currencies.models import Currency, ExchangeRate


class Command(BaseCommand):
    help = "Fetch USD-base exchange rates from Open Exchange Rates API"

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

        rates_to_create = [
            ExchangeRate(
                currency=code,
                rate=rate.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP),
                effective_date=effective_date,
            )
            for code, rate in available.items()
        ]

        ExchangeRate.objects.bulk_create(
            rates_to_create,
            update_conflicts=True,
            unique_fields=['currency', 'effective_date'],
            update_fields=['rate'],
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created/updated {len(rates_to_create)} USD-base exchange rates"
        ))
