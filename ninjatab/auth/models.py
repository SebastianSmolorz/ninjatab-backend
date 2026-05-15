from django.contrib.auth.models import AbstractUser
from django.db import models
from uuid6 import uuid7


class User(AbstractUser):
    class Platform(models.TextChoices):
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"

    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
    last_magic_link_sent_dt = models.DateTimeField(null=True, blank=True)
    before_last_magic_link_sent_dt = models.DateTimeField(null=True, blank=True)
    analytics_opted_in = models.BooleanField(default=False)
    platform = models.CharField(max_length=10, choices=Platform.choices, blank=True)
