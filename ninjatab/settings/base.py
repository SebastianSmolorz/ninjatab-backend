from pathlib import Path

from dotenv import load_dotenv
from envparse import Env

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from backend/ directory (won't override existing env vars)
load_dotenv(BASE_DIR.parent / ".env")

env = Env()

SECRET_KEY = env.str("SECRET_KEY", default="django-insecure-fallback-key-override-in-production")

DEBUG = env.bool("DEBUG", default=False)

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "ninjatab.auth",
    "ninjatab.currencies",
    "ninjatab.tabs",
]

AUTH_USER_MODEL = "ninjatab_auth.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ninjatab.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ninjatab.wsgi.application"

# Database

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CORS settings

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True

CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

# S3 / DigitalOcean Spaces
AWS_ACCESS_KEY_ID = env.str("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env.str("AWS_SECRET_ACCESS_KEY", default="")
S3_ENDPOINT = env.str("S3_ENDPOINT", default="https://ams3.digitaloceanspaces.com")
S3_BUCKET = "tab-ninja-receipt-scans"

# Mistral AI
MISTRAL_API_KEY = env.str("MISTRAL_API_KEY", default="")

# Open Exchange Rates
OPEN_EXCHANGE_RATES_APP_ID = env.str("OPEN_EXCHANGE_RATES_APP_ID", default="")

# Brevo (transactional email)
BREVO_API_KEY = env.str("BREVO_API_KEY", default="")
MAGIC_LINK_BASE_URL = env.str("MAGIC_LINK_BASE_URL", default="http://localhost:3000/auth/verify")

# Auth cookies
AUTH_COOKIE_SECURE = True

# Magic link rate limiting
MAGIC_LINK_MIN_INTERVAL = 30        # seconds
MAGIC_LINK_EXTENDED_COOLDOWN = 120   # seconds

# Free tier limits
FREE_TAB_MAX_BILLS = 7
FREE_TAB_MAX_ITEMISED_BILLS = 1

CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]
