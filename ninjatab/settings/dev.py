from .base import *  # noqa: F401, F403
from .base import env

DEBUG = True

SECRET_KEY = env.str("SECRET_KEY", default="django-insecure-3db7pm5*^nx3_#2ax=r$&mviqevr)rzyco3+m9bz38=vt=v1if")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:3000", "http://127.0.0.1:3000"],
)
