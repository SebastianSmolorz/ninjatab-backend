# models.py
from django.db import models
from django.contrib.auth.models import User
from enum import Enum

from django.db import models
from django.contrib.auth.models import User
from enum import Enum
from datetime import date


class Currency(models.TextChoices):
    USD = 'USD', 'US Dollar'
    EUR = 'EUR', 'Euro'
    GBP = 'GBP', 'British Pound'
    JPY = 'JPY', 'Japanese Yen'
    CAD = 'CAD', 'Canadian Dollar'
    TRY = 'TRY', 'Turkish Lira'


class SplitType(models.TextChoices):
    SHARES = 'shares', 'Shares'
    VALUE = 'value', 'Value'


class BillStatus(models.TextChoices):
    OPEN = 'open', 'Open'
    ALL_CLAIMED = 'all_claimed', 'All Claimed'
    ALL_PAID = 'all_paid', 'All Paid'
    ARCHIVED = 'archived', 'Archived'


class BaseModel(models.Model):
    """Base model with timestamps"""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Tab(BaseModel):
    """A tab that tracks shared expenses"""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    default_currency = models.CharField(
        max_length=3,
        choices=Currency.choices,
        default=Currency.GBP
    )
    settlement_currency = models.CharField(
        max_length=3,
        choices=Currency.choices,
        default=Currency.GBP,
        help_text="Currency used for calculating settlements"
    )
    is_settled = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def bill_count(self):
        return self.bills.count()

    @property
    def people_count(self):
        return self.people.count()


class TabPerson(BaseModel):
    """A person on a tab, optionally linked to a User"""
    tab = models.ForeignKey(
        Tab,
        on_delete=models.CASCADE,
        related_name='people'
    )
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tab_people'
    )

    class Meta:
        ordering = ['created_at']
        unique_together = [['tab', 'email']]

    def __str__(self):
        return f"{self.name} on {self.tab.name}"

    def save(self, *args, **kwargs):
        if self.email and not self.user:
            try:
                self.user = User.objects.get(email=self.email)
            except User.DoesNotExist:
                pass
        super().save(*args, **kwargs)


class Bill(BaseModel):
    """A bill/expense on a tab"""
    tab = models.ForeignKey(
        Tab,
        on_delete=models.CASCADE,
        related_name='bills'
    )
    description = models.CharField(max_length=255)
    currency = models.CharField(
        max_length=3,
        choices=Currency.choices
    )
    status = models.CharField(
        max_length=20,
        choices=BillStatus.choices,
        default=BillStatus.OPEN
    )
    creator = models.ForeignKey(
        TabPerson,
        on_delete=models.CASCADE,
        related_name='bills_created'
    )
    paid_by = models.ForeignKey(
        TabPerson,
        on_delete=models.CASCADE,
        related_name='bills_paid',
        null=True,
        blank=True
    )
    date = models.DateField(default=date.today)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.description} on {self.tab.name}"

    @property
    def total_amount(self):
        return sum(item.value for item in self.line_items.all())

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)


class LineItem(BaseModel):
    """A line item within a bill"""
    bill = models.ForeignKey(
        Bill,
        on_delete=models.CASCADE,
        related_name='line_items'
    )
    description = models.CharField(max_length=255)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    split_type = models.CharField(
        max_length=10,
        choices=SplitType.choices,
        default=SplitType.SHARES
    )

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.description} - {self.value}"


class PersonLineItemClaim(BaseModel):
    """Tracks a person's split/claim for a line item"""
    person = models.ForeignKey(
        TabPerson,
        on_delete=models.CASCADE,
        related_name='line_item_claims'
    )
    line_item = models.ForeignKey(
        LineItem,
        on_delete=models.CASCADE,
        related_name='person_claims'
    )
    split_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Number of shares or direct value depending on split_type"
    )
    calculated_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="The actual currency amount this person owes"
    )
    has_claimed = models.BooleanField(default=False)

    class Meta:
        unique_together = [['person', 'line_item']]

    def __str__(self):
        return f"{self.person.name} - {self.line_item.description}"


class Settlement(BaseModel):
    """Simplified settlement transaction showing who owes whom"""
    tab = models.ForeignKey(
        Tab,
        on_delete=models.CASCADE,
        related_name='settlements'
    )
    from_person = models.ForeignKey(
        TabPerson,
        on_delete=models.CASCADE,
        related_name='settlements_as_payer'
    )
    to_person = models.ForeignKey(
        TabPerson,
        on_delete=models.CASCADE,
        related_name='settlements_as_payee'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(
        max_length=3,
        choices=Currency.choices
    )
    paid = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.from_person.name} pays {self.to_person.name} {self.amount} {self.currency}"


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