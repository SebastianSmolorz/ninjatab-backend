import os

from .base import *  # noqa: F401, F403
from .base import env

DEBUG = False

# Required in production — will crash on startup if missing
SECRET_KEY = os.environ["SECRET_KEY"]

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS")

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Security — assume reverse proxy terminates SSL
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

STATIC_ROOT = "/var/www/ninjatab/static"
MEDIA_ROOT = "/var/www/ninjatab/media"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "file": {
            "level": "ERROR",
            "class": "logging.FileHandler",
            "filename": "/var/log/django/error.log",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["file"],
            "level": "ERROR",
            "propagate": True,
        },
    },
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["DB_NAME"],
        "USER": os.environ["DB_USER"],
        "PASSWORD": os.environ["DB_PASSWORD"],
        "HOST": os.environ["DB_HOST"],
        "PORT": os.environ.get("DB_PORT", "5432"),

        "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "30")),
        "CONN_HEALTH_CHECKS": True,

        "OPTIONS": {
            "sslmode": os.environ.get("DB_SSLMODE", "require"),
        },
    }
}
DISABLE_SERVER_SIDE_CURSORS = True
