# exchange.py
"""Helper functions for currency conversion using exchange rates"""

from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from .models import ExchangeRate
from .currency_utils import minor_to_minor


class ExchangeRateNotFoundError(Exception):
    """Raised when no exchange rate is found for a currency pair"""
    pass


_rate_cache = {}


def clear_rate_cache():
    """Clear the in-memory exchange rate cache. Call at the start of a request or operation."""
    _rate_cache.clear()


def _usd_rate(currency: str, as_of_date: datetime) -> Decimal:
    """Return rate such that 1 USD = rate * currency, as of the given date."""
    if currency == 'USD':
        return Decimal('1')

    rate = ExchangeRate.objects.filter(
        currency=currency,
        effective_date__lte=as_of_date,
    ).order_by('-effective_date').first()

    if rate is None:
        raise ExchangeRateNotFoundError(
            f"No USD-base exchange rate found for {currency} "
            f"as of {as_of_date.date()}"
        )
    return rate.rate


def get_latest_exchange_rate(from_currency: str, to_currency: str, as_of_date: datetime = None) -> Decimal:
    """
    Get the latest exchange rate from one currency to another.

    Computed from USD-base rates: rate = usd_rate(to) / usd_rate(from), so
    1 from_currency = rate * to_currency.

    Args:
        from_currency: Source currency code (e.g., 'USD')
        to_currency: Target currency code (e.g., 'GBP')
        as_of_date: Optional datetime to get historical rate. If None, uses current time.

    Returns:
        Decimal: Exchange rate (1 from_currency = rate * to_currency)

    Raises:
        ExchangeRateNotFoundError: If no rate is found for either currency
    """
    if from_currency == to_currency:
        return Decimal('1.0')

    if as_of_date is None:
        as_of_date = timezone.now()

    cache_key = (from_currency, to_currency, as_of_date.date())
    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    from_usd = _usd_rate(from_currency, as_of_date)
    if abs(from_usd) < Decimal('0.000001'):
        raise ExchangeRateNotFoundError(
            f"USD-base rate for {from_currency} is too small to invert"
        )
    to_usd = _usd_rate(to_currency, as_of_date)

    rate = to_usd / from_usd
    _rate_cache[cache_key] = rate
    return rate


def convert_amount(
    amount: int,
    from_currency: str,
    to_currency: str,
    as_of_date: datetime = None
) -> int:
    """
    Convert an amount (in minor units of from_currency) to minor units of to_currency.

    Args:
        amount: Amount in minor currency units (e.g. cents for USD)
        from_currency: Source currency code
        to_currency: Target currency code
        as_of_date: Optional datetime for historical conversion

    Returns:
        int: Converted amount in minor units of to_currency

    Raises:
        ExchangeRateNotFoundError: If no rate is found for the currency pair
    """
    if from_currency == to_currency:
        return amount

    rate = get_latest_exchange_rate(from_currency, to_currency, as_of_date)
    return minor_to_minor(amount, from_currency, to_currency, rate)
