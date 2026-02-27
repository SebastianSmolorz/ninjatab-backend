from django.contrib.auth.models import AbstractUser
from django.db import models
from uuid6 import uuid7


class User(AbstractUser):
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False, db_index=True)
