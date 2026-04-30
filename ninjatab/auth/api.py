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
    SocialLoginSchema,
)
from ninjatab.auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    create_magic_token,
    decode_token,
)
from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.email import send_magic_link, send_deletion_request_email
from ninjatab.auth.rate_limit import check_magic_link_rate_limit
from django.conf import settings as django_settings
from django.utils import timezone
from ninjatab.auth.cookies import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    set_auth_cookies,
    clear_auth_cookies,
)
from ninjatab.auth.social import verify_google_id_token, verify_apple_id_token
import logging
from datetime import timedelta
from posthog import new_context, identify_context, capture as ph_capture

logger = logging.getLogger("app")
gdpr_logger = logging.getLogger("gdpr")

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
    is_demo = payload.email.lower() == 'demo@tab.ninja'
    if payload.skip_email and (django_settings.DEBUG or is_demo):
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

    with new_context():
        identify_context(str(user.uuid))
        is_new = (timezone.now() - user.date_joined) < timedelta(minutes=5)
        ph_capture("user_signed_up" if is_new else "user_logged_in", properties={
            "method": "magic_link",
        })

    return response


@auth_router.post("/social-login", response=TokenResponseSchema)
def social_login(request, payload: SocialLoginSchema):
    if payload.provider not in ("google", "apple"):
        raise HttpError(400, "Unsupported provider")

    logger.info("Social login attempt: provider=%s", payload.provider)

    try:
        if payload.provider == "google":
            provider_data = verify_google_id_token(payload.id_token)
        else:
            provider_data = verify_apple_id_token(payload.id_token)
        logger.info("Token verified: email=%s", provider_data.get("email"))
    except Exception as e:
        logger.error("Social login token verification failed: provider=%s, error_type=%s, error=%s", payload.provider, type(e).__name__, str(e), exc_info=True)
        raise HttpError(401, "Invalid or expired token")

    email = provider_data["email"].lower()
    user, created = User.objects.get_or_create(
        email=email,
        defaults={"username": email},
    )

    # Populate name if available and not already set
    first_name = provider_data.get("first_name") or payload.first_name or ""
    last_name = provider_data.get("last_name") or payload.last_name or ""
    updated_fields = []
    if first_name and not user.first_name:
        user.first_name = first_name
        updated_fields.append("first_name")
    if last_name and not user.last_name:
        user.last_name = last_name
        updated_fields.append("last_name")
    if updated_fields:
        user.save(update_fields=updated_fields)

    if not user.is_active:
        raise HttpError(403, "Account is blocked")

    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)

    user_schema = AuthUserSchema.model_validate(user)
    response = JsonResponse({"user": user_schema.model_dump()})
    set_auth_cookies(response, access_token, refresh_token)

    with new_context():
        identify_context(str(user.uuid))
        ph_capture("user_signed_up" if created else "user_logged_in", properties={
            "method": payload.provider,
        })

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
    new_refresh_token = create_refresh_token(user.id)

    response = JsonResponse({"success": True})
    set_auth_cookies(response, new_access_token, new_refresh_token)
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


@auth_router.post("/me/request-deletion", response=LogoutResponseSchema, auth=JWTBearer())
def request_deletion(request):
    user = request.auth
    gdpr_logger.info("account_deletion_requested user_id=%s email=%s", user.id, user.email)
    send_deletion_request_email(user.id, user.email)
    return {"success": True}
