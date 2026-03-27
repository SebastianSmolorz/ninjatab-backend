from django.conf import settings

ACCESS_COOKIE = "ninjatab_access_token"
REFRESH_COOKIE = "ninjatab_refresh_token"


def set_auth_cookies(response, access_token, refresh_token):
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="Lax",
        max_age=24 * 3600,
        path="/api/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="Lax",
        max_age=30 * 24 * 3600,
        path="/api/auth/refresh",
    )


def clear_auth_cookies(response):
    response.delete_cookie(ACCESS_COOKIE, path="/api/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth/refresh")
