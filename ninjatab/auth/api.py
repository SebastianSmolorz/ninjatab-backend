import jwt
from ninja import Router
from ninja.errors import HttpError
from django.contrib.auth import get_user_model
from django.http import JsonResponse

from ninjatab.auth.schemas import (
    MagicLinkSchema,
    MagicLinkSuccessSchema,
    VerifyMagicLinkSchema,
    TokenResponseSchema,
    AuthUserSchema,
    RefreshResponseSchema,
    LogoutResponseSchema,
    UpdateProfileSchema,
)
from ninjatab.auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    create_magic_token,
    decode_token,
)
from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.email import send_magic_link
from ninjatab.auth.rate_limit import check_magic_link_rate_limit
from django.conf import settings as django_settings
from django.utils import timezone
from ninjatab.auth.cookies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    set_auth_cookies,
    clear_auth_cookies,
)

User = get_user_model()

auth_router = Router(tags=["auth"])


@auth_router.post("/magic-link", response=MagicLinkSuccessSchema)
def magic_link(request, payload: MagicLinkSchema):
    user, _ = User.objects.get_or_create(
        email=payload.email.lower(),
        defaults={"username": payload.email.lower()},
    )
    check_magic_link_rate_limit(user)
    token = create_magic_token(user.id)
    magic_url = f"{django_settings.MAGIC_LINK_BASE_URL}?token={token}"
    if payload.skip_email and django_settings.DEBUG:
        return {"success": True, "magic_url": magic_url}
    send_magic_link(payload.email.lower(), token)
    user.before_last_magic_link_sent_dt = user.last_magic_link_sent_dt
    user.last_magic_link_sent_dt = timezone.now()
    user.save(update_fields=["last_magic_link_sent_dt", "before_last_magic_link_sent_dt"])
    return {"success": True}


@auth_router.post("/verify-magic-link", response=TokenResponseSchema)
def verify_magic_link(request, payload: VerifyMagicLinkSchema):
    try:
        token_data = decode_token(payload.token)
        if token_data.get("type") != "magic":
            raise HttpError(401, "Invalid token type")
        user = User.objects.get(id=int(token_data["sub"]))
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise HttpError(401, "Invalid or expired magic link")
    except User.DoesNotExist:
        raise HttpError(401, "User not found")

    if not user.is_active:
        raise HttpError(403, "Account is blocked")

    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)

    user_schema = AuthUserSchema.model_validate(user)
    response = JsonResponse({"user": user_schema.model_dump()})
    set_auth_cookies(response, access_token, refresh_token)
    return response


@auth_router.post("/refresh", response=RefreshResponseSchema)
def refresh(request):
    raw = request.COOKIES.get(REFRESH_COOKIE)
    if not raw:
        raise HttpError(401, "No refresh token")

    try:
        token_data = decode_token(raw)
        if token_data.get("type") != "refresh":
            raise HttpError(401, "Invalid token type")
        user = User.objects.get(id=int(token_data["sub"]))
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise HttpError(401, "Invalid or expired refresh token")
    except User.DoesNotExist:
        raise HttpError(401, "User not found")

    if not user.is_active:
        raise HttpError(403, "Account is blocked")

    new_access_token = create_access_token(user.id, user.email)

    response = JsonResponse({"success": True})
    response.set_cookie(
        ACCESS_COOKIE,
        new_access_token,
        httponly=True,
        secure=django_settings.AUTH_COOKIE_SECURE,
        samesite="Lax",
        max_age=24 * 3600,
        path="/api/",
    )
    return response


@auth_router.post("/logout", response=LogoutResponseSchema)
def logout(request):
    response = JsonResponse({"success": True})
    clear_auth_cookies(response)
    return response


@auth_router.get("/me", response=AuthUserSchema, auth=JWTBearer())
def me(request):
    return request.auth


@auth_router.patch("/me", response=AuthUserSchema, auth=JWTBearer())
def update_me(request, payload: UpdateProfileSchema):
    user = request.auth
    user.first_name = payload.first_name.strip()
    user.save(update_fields=["first_name"])
    return user
