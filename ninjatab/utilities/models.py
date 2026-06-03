from django.db import models
from uuid6 import uuid7


class BaseModel(models.Model):
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Option(BaseModel):
    """A named, runtime-configurable application setting.

    Options are defined in ``registry.OPTION_REGISTRY`` and synced to the
    database via the ``sync_options`` management command (or get_or_create at
    runtime). Inherits a default numeric ``id`` plus created/updated timestamps
    from :class:`BaseModel`.
    """

    name = models.CharField(max_length=100, unique=True, db_index=True)
    active = models.BooleanField(default=False)
    value = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({'active' if self.active else 'inactive'})"


class AppMessage(BaseModel):
    class Level(models.TextChoices):
        INFO = 'info', 'Info'
        WARNING = 'warning', 'Warning'
        ERROR = 'error', 'Error'

    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    message = models.TextField()
    active = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.level}] {self.message[:60]}"
