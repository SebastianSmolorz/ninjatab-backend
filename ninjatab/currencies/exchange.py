# exchange.py
"""Helper functions for currency conversion using exchange rates"""

from decimal import Decimal
from datetime import datetime
from functools import lru_cache
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


def get_latest_exchange_rate(from_currency: str, to_currency: str, as_of_date: datetime = None) -> Decimal:
    """
    Get the latest exchange rate from one currency to another.

    Args:
        from_currency: Source currency code (e.g., 'USD')
        to_currency: Target currency code (e.g., 'GBP')
        as_of_date: Optional datetime to get historical rate. If None, uses current time.

    Returns:
        Decimal: Exchange rate (1 from_currency = rate * to_currency)

    Raises:
        ExchangeRateNotFoundError: If no rate is found for the currency pair
    """
    # If converting to same currency, return 1
    if from_currency == to_currency:
        return Decimal('1.0')

    if as_of_date is None:
        as_of_date = timezone.now()

    # Check in-memory cache (avoids repeated DB hits within the same operation)
    cache_key = (from_currency, to_currency, as_of_date.date())
    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    # Try to find direct rate (from -> to)
    try:
        rate = ExchangeRate.objects.filter(
            from_currency=from_currency,
            to_currency=to_currency,
            effective_date__lte=as_of_date
        ).order_by('-effective_date').first()

        if rate:
            _rate_cache[cache_key] = rate.rate
            return rate.rate
    except ExchangeRate.DoesNotExist:
        pass

    # Try inverse rate (to -> from)
    try:
        rate = ExchangeRate.objects.filter(
            from_currency=to_currency,
            to_currency=from_currency,
            effective_date__lte=as_of_date
        ).order_by('-effective_date').first()

        if rate and rate.rate != 0:
            # Protect against near-zero rates that would cause huge inverses
            if abs(rate.rate) < Decimal('0.000001'):
                raise ExchangeRateNotFoundError(
                    f"Exchange rate for {to_currency} to {from_currency} is too small to invert"
                )
            inverse = Decimal('1.0') / rate.rate
            _rate_cache[cache_key] = inverse
            return inverse
    except ExchangeRate.DoesNotExist:
        pass

    raise ExchangeRateNotFoundError(
        f"No exchange rate found for {from_currency} to {to_currency} "
        f"as of {as_of_date.date()}"
    )


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
