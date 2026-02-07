"""
ASGI config for ninjatab project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from django.core.asgi import get_asgi_application

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ninjatab.settings.prod")

application = get_asgi_application()
