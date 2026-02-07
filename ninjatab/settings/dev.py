from .base import *  # noqa: F401, F403

DEBUG = True

SECRET_KEY = "django-insecure-3db7pm5*^nx3_#2ax=r$&mviqevr)rzyco3+m9bz38=vt=v1if"

ALLOWED_HOSTS = ["*"]

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
