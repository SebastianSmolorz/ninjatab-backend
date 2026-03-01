import logging

from django.conf import settings
from django.utils import timezone
from ninja.errors import HttpError

logger = logging.getLogger("ninjatab.auth.rate_limit")


def check_magic_link_rate_limit(user):
    min_interval = getattr(settings, "MAGIC_LINK_MIN_INTERVAL", None)
    extended_cooldown = getattr(settings, "MAGIC_LINK_EXTENDED_COOLDOWN", None)

    if min_interval is None or extended_cooldown is None:
        return

    if user.last_magic_link_sent_dt is None:
        return

    now = timezone.now()
    elapsed = (now - user.last_magic_link_sent_dt).total_seconds()

    if elapsed < min_interval:
        raise HttpError(429, "Please wait before requesting another magic link.")

    if (
        user.before_last_magic_link_sent_dt is not None
        and (user.last_magic_link_sent_dt - user.before_last_magic_link_sent_dt).total_seconds() < 60
        and elapsed < extended_cooldown
    ):
        logger.warning(
            "Repeated magic link requests for user %s (id=%s)", user.email, user.id
        )
        raise HttpError(429, "Too many requests. Please try again later.")
