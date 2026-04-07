from decimal import Decimal

CURRENCY_DECIMAL_PLACES: dict[str, int] = {
    'JPY': 0,
    'HUF': 0,
}


def get_decimal_places(currency_code: str) -> int:
    return CURRENCY_DECIMAL_PLACES.get(currency_code, 2)


def minor_to_decimal(amount_minor: int | None, currency_code: str) -> Decimal | None:
    """Convert integer minor units to display Decimal. E.g. 1050, 'USD' → Decimal('10.50')"""
    if amount_minor is None:
        return None
    dp = get_decimal_places(currency_code)
    if dp == 0:
        return Decimal(amount_minor)
    return Decimal(amount_minor) / Decimal(10 ** dp)


def decimal_to_minor(amount: Decimal, currency_code: str) -> int:
    """Convert display Decimal to integer minor units. E.g. Decimal('10.50'), 'USD' → 1050"""
    dp = get_decimal_places(currency_code)
    return int(round(amount * 10 ** dp))


def minor_to_minor(amount_minor: int, from_currency: str, to_currency: str, rate: Decimal) -> int:
    """Convert minor units from one currency to another using a Decimal exchange rate.
    Handles currencies with differing decimal places (e.g. USD→JPY).
    """
    from_dp = get_decimal_places(from_currency)
    to_dp = get_decimal_places(to_currency)
    result = Decimal(amount_minor) / Decimal(10 ** from_dp) * rate * Decimal(10 ** to_dp)
    return int(result.quantize(Decimal('1')))
