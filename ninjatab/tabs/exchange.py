# exchange.py
"""Helper functions for currency conversion using exchange rates"""

from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from .models import ExchangeRate


class ExchangeRateNotFoundError(Exception):
    """Raised when no exchange rate is found for a currency pair"""
    pass


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

    # Try to find direct rate (from -> to)
    try:
        rate = ExchangeRate.objects.filter(
            from_currency=from_currency,
            to_currency=to_currency,
            effective_date__lte=as_of_date
        ).first()

        if rate:
            return rate.rate
    except ExchangeRate.DoesNotExist:
        pass

    # Try inverse rate (to -> from)
    try:
        rate = ExchangeRate.objects.filter(
            from_currency=to_currency,
            to_currency=from_currency,
            effective_date__lte=as_of_date
        ).first()

        if rate and rate.rate != 0:
            return Decimal('1.0') / rate.rate
    except ExchangeRate.DoesNotExist:
        pass

    raise ExchangeRateNotFoundError(
        f"No exchange rate found for {from_currency} to {to_currency} "
        f"as of {as_of_date.date()}"
    )


def convert_amount(
    amount: Decimal,
    from_currency: str,
    to_currency: str,
    as_of_date: datetime = None
) -> Decimal:
    """
    Convert an amount from one currency to another using exchange rates.

    Args:
        amount: Amount to convert
        from_currency: Source currency code
        to_currency: Target currency code
        as_of_date: Optional datetime for historical conversion

    Returns:
        Decimal: Converted amount quantized to 2 decimal places

    Raises:
        ExchangeRateNotFoundError: If no rate is found for the currency pair
    """
    if from_currency == to_currency:
        return amount.quantize(Decimal('0.01'))

    rate = get_latest_exchange_rate(from_currency, to_currency, as_of_date)
    converted = amount * rate

    # Quantize to 2 decimal places for currency
    return converted.quantize(Decimal('0.01'))
