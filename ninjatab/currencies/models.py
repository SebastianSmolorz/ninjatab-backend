from django.db import models


class Currency(models.TextChoices):
    USD = 'USD', 'US Dollar'
    EUR = 'EUR', 'Euro'
    GBP = 'GBP', 'British Pound'
    JPY = 'JPY', 'Japanese Yen'
    CAD = 'CAD', 'Canadian Dollar'
    TRY = 'TRY', 'Turkish Lira'
    PLN = 'PLN', 'Polish Złoty'
    CZK = 'CZK', 'Czech Crown'
    AUD = 'AUD', 'Australian Dollar'
    CHF = 'CHF', 'Swiss Franc'
    HUF = 'HUF', 'Hungarian Forint'
    BGN = 'BGN', 'Bulgarian Lev'
    MXN = 'MXN', 'Mexican Peso'
    THB = 'THB', 'Thai Baht'


class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ExchangeRate(BaseModel):
    """Exchange rate from one currency to another with historical tracking"""
    from_currency = models.CharField(
        max_length=3,
        choices=Currency.choices
    )
    to_currency = models.CharField(
        max_length=3,
        choices=Currency.choices
    )
    rate = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        help_text="Exchange rate: 1 from_currency = rate * to_currency"
    )
    effective_date = models.DateTimeField(
        help_text="Date and time when this rate became effective"
    )

    class Meta:
        ordering = ['-effective_date']
        unique_together = [['from_currency', 'to_currency', 'effective_date']]
        indexes = [
            models.Index(fields=['from_currency', 'to_currency', '-effective_date']),
        ]

    def __str__(self):
        return f"1 {self.from_currency} = {self.rate} {self.to_currency} (effective {self.effective_date.date()})"
