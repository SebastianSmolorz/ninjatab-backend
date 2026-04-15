import atexit

from django.apps import AppConfig
from django.conf import settings


class TabsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ninjatab.tabs"

    def ready(self):
        import posthog

        posthog.api_key = settings.POSTHOG_PROJECT_TOKEN
        posthog.host = settings.POSTHOG_HOST

        if settings.POSTHOG_DISABLED:
            posthog.disabled = True

        if settings.DEBUG:
            posthog.debug = True

        atexit.register(posthog.shutdown)
