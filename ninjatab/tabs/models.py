# models.py
import uuid
from django.db import models
from django.db.models import Q
from django.conf import settings
from enum import Enum
from datetime import date
from uuid6 import uuid7
from ninjatab.currencies.currency_utils import minor_to_decimal
from ninjatab.currencies.models import Currency  # re-exported for backward compatibility


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


class VersionedModel(models.Model):
    """Mixin adding an optimistic-concurrency version counter.

    `version` starts at 1 on create and increments on every subsequent save, so
    a client can read it, send it back with an edit, and detect when it was
    editing against stale data (the basis for offline-edit conflict detection).

    Caveat: this hooks `save()`, so it is bypassed by `QuerySet.update()` and
    `bulk_update()`, which don't call `save()`. Mutate through model instances
    to keep the counter accurate; for changes that only touch child rows (e.g.
    submitting splits, which rewrites claims), bump the parent explicitly.
    """
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self.version = (self.version or 0) + 1
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "version"}
        super().save(*args, **kwargs)



class TabManager(models.Manager):
    def accessible_by(self, user):
        # A user can reach a tab if they own it, are a person on it, or are a
        # member of the house (TabGroup) it belongs to. The group clause grants
        # a member access to every period of the house, including ones that
        # predate them joining.
        return self.filter(
            Q(created_by=user)
            | Q(people__user=user)
            | Q(group__members__user=user)
        ).distinct()


class TabGroupManager(models.Manager):
    def accessible_by(self, user):
        return self.filter(
            Q(created_by=user) | Q(members__user=user)
        ).distinct()


class TabGroup(BaseModel):
    """An ongoing "house" that owns a sequence of period Tabs.

    Each settlement period is a regular Tab linked via ``Tab.group``. Settling a
    period closes its Tab and spawns a fresh one with the roster copied in, so a
    house retains the same people month after month while each period stays an
    immutable record of its own spend.
    """
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_groups',
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
    is_archived = models.BooleanField(default=False)
    invite_code = models.UUIDField(default=uuid.uuid4, unique=True, null=True, blank=True)

    objects = TabGroupManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def rotate_invite_code(self):
        self.invite_code = uuid.uuid4()
        self.save(update_fields=["invite_code"])

    @property
    def current_period(self):
        """The single open, non-archived period Tab, or None."""
        return (
            self.tabs.filter(is_settled=False, is_archived=False)
            .order_by('-created_at')
            .first()
        )


class TabGroupMember(BaseModel):
    """Canonical roster entry for a house, optionally linked to a User.

    This is the source of truth for "who is in the house". Each period's
    TabPerson rows are projected from these members at roll time.
    """
    group = models.ForeignKey(
        TabGroup,
        on_delete=models.CASCADE,
        related_name='members'
    )
    name = models.CharField(max_length=255)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='group_memberships'
    )

    class Meta:
        ordering = ['created_at']
        unique_together = [['group', 'name']]
        indexes = [
            models.Index(fields=['group', 'user']),
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return f"{self.name} in {self.group.name}"


class Tab(VersionedModel, BaseModel):
    """A tab that tracks shared expenses"""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    group = models.ForeignKey(
        TabGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tabs',
        help_text="The house this tab is a settlement period of (null for standalone tabs)"
    )
    period_index = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="1-based position of this period within its house (display only)"
    )
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
    is_archived = models.BooleanField(default=False)
    settlement_currency_settled_total = models.IntegerField(
        null=True, blank=True,
        help_text="Total spent in settlement currency (minor units), snapshotted at settlement time"
    )
    is_pro = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False)
    receipt_scan_count = models.PositiveIntegerField(default=0)
    invite_code = models.UUIDField(default=uuid.uuid4, unique=True, null=True, blank=True)

    objects = TabManager()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at', '-id']),
        ]
        constraints = [
            # A house has at most one open period at a time.
            models.UniqueConstraint(
                fields=['group'],
                condition=Q(group__isnull=False, is_settled=False, is_archived=False),
                name='uniq_active_period_per_group',
            ),
        ]

    def __str__(self):
        return self.name

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
    member = models.ForeignKey(
        'TabGroupMember',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tab_people',
        help_text="The house member this period person was projected from (null for standalone tabs)"
    )

    class Meta:
        ordering = ['created_at']
        unique_together = [['tab', 'name']]
        indexes = [
            models.Index(fields=['tab', 'user']),
        ]

    def __str__(self):
        return f"{self.name} on {self.tab.name}"


class Bill(VersionedModel, BaseModel):
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
    receipt_image_url = models.URLField(max_length=500, blank=True, default='')
    receipt_image_key = models.CharField(max_length=500, blank=True, default='')
    # Client-supplied idempotency key (the app's offline-queue localId). Lets a
    # retried create — e.g. after a crash between server-create and client-ack —
    # return the original bill instead of creating a duplicate.
    client_id = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['-date', '-id']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['creator', 'client_id'],
                condition=Q(client_id__isnull=False),
                name='uniq_bill_creator_client_id',
            ),
        ]

    def __str__(self):
        return f"{self.description} on {self.tab.name}"

    @property
    def is_itemised(self):
        return self.line_items.count() > 1

    @property
    def total_amount(self):
        return sum(item.value for item in self.line_items.all())


class LineItem(VersionedModel, BaseModel):
    """A line item within a bill"""
    bill = models.ForeignKey(
        Bill,
        on_delete=models.CASCADE,
        related_name='line_items'
    )
    description = models.CharField(max_length=255)
    translated_name = models.CharField(max_length=255, blank=True, default='')
    value = models.IntegerField()
    split_type = models.CharField(
        max_length=10,
        choices=SplitType.choices,
        default=SplitType.SHARES
    )

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        amount = minor_to_decimal(self.value, self.bill.currency)
        return f"{self.description} - {amount} {self.bill.currency}"


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
    split_value = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of shares (SHARES mode) or minor currency units (VALUE mode)"
    )
    calculated_amount = models.IntegerField(
        null=True,
        blank=True,
        help_text="The actual currency amount this person owes, in minor units"
    )
    settlement_amount = models.IntegerField(
        null=True,
        blank=True,
        help_text="calculated_amount converted to the tab's settlement_currency, in minor units"
    )
    has_claimed = models.BooleanField(default=False)

    class Meta:
        unique_together = [['person', 'line_item']]

    def __str__(self):
        if self.calculated_amount is not None:
            currency = self.line_item.bill.currency
            amount = minor_to_decimal(self.calculated_amount, currency)
            return f"{self.person.name} - {self.line_item.description} ({amount} {currency})"
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
    amount = models.IntegerField()
    currency = models.CharField(
        max_length=3,
        choices=Currency.choices
    )
    paid = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tab', 'paid']),
        ]

    def __str__(self):
        amount = minor_to_decimal(self.amount, self.currency)
        return f"{self.from_person.name} pays {self.to_person.name} {amount} {self.currency}"


