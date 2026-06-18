from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from uuid6 import uuid7

from ninjatab.utilities.fields import EncryptedCharField


class User(AbstractUser):
    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"

    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
    last_magic_link_sent_dt = models.DateTimeField(null=True, blank=True)
    before_last_magic_link_sent_dt = models.DateTimeField(null=True, blank=True)
    analytics_opted_in = models.BooleanField(default=False)
    platform = models.CharField(max_length=10, choices=Platform.choices, blank=True)


class UserPaymentMethod(models.Model):
    """A personal payment handle (PayPal/Monzo/Revolut) for a user.

    The username is encrypted at rest. Reachable from a tab via
    ``tab_person.user.payment_methods``. At most one row per user may be the
    preferred method, enforced by a partial unique constraint.
    """

    class Provider(models.TextChoices):
        PAYPAL = "paypal", "PayPal"
        MONZO = "monzo", "Monzo"
        REVOLUT = "revolut", "Revolut"
        CASHAPP = "cashapp", "Cash App"
        VENMO = "venmo", "Venmo"

    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payment_methods",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    username = EncryptedCharField()
    is_preferred = models.BooleanField(default=False)

    class Meta:
        ordering = ["provider"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"],
                name="uniq_user_payment_provider",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_preferred=True),
                name="uniq_preferred_payment_method_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.get_provider_display()} for {self.user}"
