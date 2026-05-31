"""Column-level encrypted model fields.

Values are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256) using the
``FIELD_ENCRYPTION_KEY`` setting. Plaintext is exposed transparently to Python
code; only ciphertext is written to the database. Because Fernet output is
non-deterministic, encrypted columns cannot be filtered or looked up by value.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be set to use encrypted fields")
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedCharField(models.TextField):
    """A text column whose contents are encrypted at rest with Fernet."""

    description = "Fernet-encrypted text"

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        try:
            return _get_fernet().decrypt(value.encode()).decode()
        except (InvalidToken, ValueError):
            # Not decryptable (e.g. legacy plaintext) — surface as-is rather
            # than blowing up reads.
            return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None:
            return value
        return _get_fernet().encrypt(value.encode()).decode()
