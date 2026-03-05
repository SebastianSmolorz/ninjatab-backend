# models.py
import uuid
from django.db import models
from django.db.models import Q
from django.conf import settings
from enum import Enum
from datetime import date
from uuid6 import uuid7


class Currency(models.TextChoices):
    USD = 'USD', 'US Dollar'
    EUR = 'EUR', 'Euro'
    GBP = 'GBP', 'British Pound'
    JPY = 'JPY', 'Japanese Yen'
    CAD = 'CAD', 'Canadian Dollar'
    TRY = 'TRY', 'Turkish Lira'
    PLN = 'PLN', 'Polish Złoty'
    CZK = 'CZK', 'Czech Crown'


class SplitType(models.TextChoices):
    SHARES = 'shares', 'Shares'
    VALUE = 'value', 'Value'


class BillStatus(models.TextChoices):
    OPEN = 'open', 'Open'
    ARCHIVED = 'archived', 'Archived'


class BaseModel(models.Model):
    """Base model with timestamps"""
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TabManager(models.Manager):
    def accessible_by(self, user):
        return self.filter(
            Q(created_by=user) | Q(people__user=user)
        ).distinct()


class Tab(BaseModel):
    """A tab that tracks shared expenses"""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_tabs',
        null=True,
        blank=True
    )
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
    is_pro = models.BooleanField(default=False)
    invite_code = models.UUIDField(default=uuid.uuid4, unique=True)

    objects = TabManager()

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

    def rotate_invite_code(self):
        self.invite_code = uuid.uuid4()
        self.save(update_fields=["invite_code"])


class TabPerson(BaseModel):
    """A person on a tab, optionally linked to a User"""
    tab = models.ForeignKey(
        Tab,
        on_delete=models.CASCADE,
        related_name='people'
    )
    name = models.CharField(max_length=255)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tab_people'
    )

    class Meta:
        ordering = ['created_at']
        unique_together = [['tab', 'name']]

    def __str__(self):
        return f"{self.name} on {self.tab.name}"


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
    def is_itemised(self):
        return self.line_items.count() > 1

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


class Contact(BaseModel):
    """Tracks a contact relationship between two users (built from shared tab history)"""
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='contacts'
    )
    contact_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='contact_of'
    )

    class Meta:
        unique_together = [['owner', 'contact_user']]
        ordering = ['contact_user__first_name', 'contact_user__last_name']

    def __str__(self):
        return f"{self.owner} → {self.contact_user}"


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


