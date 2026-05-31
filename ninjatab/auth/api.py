import jwt
from ninja import Router
from ninja.errors import HttpError
from django.contrib.auth import get_user_model
from django.db import transaction
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
    PaymentMethodSchema,
    PaymentMethodUpsertSchema,
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
from ninjatab.tabs.demo import create_demo_tab
import logging
import sentry_sdk
from datetime import timedelta
from ninjatab.utilities.analytics import safe_capture, safe_identify


def _identify_user(user, method):
    try:
        distinct_id = getattr(user, "uuid", None)
        if distinct_id is None:
            return
        opted_in = bool(getattr(user, "analytics_opted_in", False))
        properties = {
            "platform": getattr(user, "platform", None) or None,
            "last_login_method": method,
            "analytics_opted_in": opted_in,
        }
        if opted_in:
            first = getattr(user, "first_name", "") or ""
            last = getattr(user, "last_name", "") or ""
            properties["email"] = getattr(user, "email", None) or None
            properties["name"] = f"{first} {last}".strip() or None
            properties["first_name"] = first or None
            properties["last_name"] = last or None
        else:
            # Clear any previously-set PII if the user has since opted out.
            properties["email"] = None
            properties["name"] = None
            properties["first_name"] = None
            properties["last_name"] = None
        date_joined = getattr(user, "date_joined", None)
        set_once = {
            "initial_platform": getattr(user, "platform", None) or None,
            "signup_method": method,
            "date_joined": date_joined.isoformat() if date_joined else None,
        }
        safe_identify(distinct_id, properties=properties, set_once=set_once)
    except Exception as e:
        logger.exception("Failed to build analytics identify payload")
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            logger.exception("Failed to report analytics failure to Sentry")

logger = logging.getLogger("app")
gdpr_logger = logging.getLogger("gdpr")

User = get_user_model()

auth_router = Router(tags=["auth"])


@auth_router.post("/magic-link", response=MagicLinkSuccessSchema)
def magic_link(request, payload: MagicLinkSchema):
    defaults = {"username": payload.email.lower()}
    if payload.platform in User.Platform.values:
        defaults["platform"] = payload.platform
    user, created = User.objects.get_or_create(
        email=payload.email.lower(),
        defaults=defaults,
    )
    if created:
        try:
            with transaction.atomic():
                create_demo_tab(user)
        except Exception:
            logger.exception("Failed to create demo tab on signup user_id=%s", user.id)
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

    logger.info("Magic link sent email=%s", payload.email.lower())

    safe_capture(user.uuid, "magic_link_requested", properties={"method": "magic_link"})

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

    is_new = (timezone.now() - user.date_joined) < timedelta(minutes=5)
    _identify_user(user, "magic_link")
    safe_capture(user.uuid, "user_signed_up" if is_new else "user_logged_in", properties={
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
        safe_capture("$anon", "social_login_failed", properties={
            "provider": payload.provider,
            "reason_bucket": type(e).__name__,
        })
        raise HttpError(401, "Invalid or expired token")

    email = provider_data["email"].lower()
    defaults = {"username": email}
    if payload.platform in User.Platform.values:
        defaults["platform"] = payload.platform
    user, created = User.objects.get_or_create(
        email=email,
        defaults=defaults,
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

    if created:
        try:
            with transaction.atomic():
                create_demo_tab(user)
        except Exception:
            logger.exception("Failed to create demo tab on signup user_id=%s", user.id)

    if not user.is_active:
        raise HttpError(403, "Account is blocked")

    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)

    user_schema = AuthUserSchema.model_validate(user)
    response = JsonResponse({"user": user_schema.model_dump(), "is_new": created})
    set_auth_cookies(response, access_token, refresh_token)

    _identify_user(user, payload.provider)
    safe_capture(user.uuid, "user_signed_up" if created else "user_logged_in", properties={
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
        safe_capture("$anon", "auth_refresh_failed", properties={"reason": "invalid_token"})
        raise HttpError(401, "Invalid or expired refresh token")
    except User.DoesNotExist:
        safe_capture("$anon", "auth_refresh_failed", properties={"reason": "user_not_found"})
        raise HttpError(401, "User not found")

    if not user.is_active:
        raise HttpError(403, "Account is blocked")

    new_access_token = create_access_token(user.id, user.email)
    new_refresh_token = create_refresh_token(user.id)

    response = JsonResponse({"success": True})
    set_auth_cookies(response, new_access_token, new_refresh_token)
    _identify_user(user, "refresh")
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
    from ninjatab.tabs.models import TabPerson
    user = request.auth
    user.first_name = payload.first_name.strip()
    update_fields = ["first_name"]
    opt_in_changed = False
    if payload.analytics_opted_in is not None and payload.analytics_opted_in != user.analytics_opted_in:
        user.analytics_opted_in = payload.analytics_opted_in
        update_fields.append("analytics_opted_in")
        opt_in_changed = True
    user.save(update_fields=update_fields)

    new_name = user.first_name.strip() or "You"
    TabPerson.objects.filter(user=user, tab__is_demo=True).update(name=new_name)

    if opt_in_changed:
        _identify_user(user, "profile_update")

    return user


@auth_router.post("/me/request-deletion", response=LogoutResponseSchema, auth=JWTBearer())
def request_deletion(request):
    user = request.auth
    gdpr_logger.info("account_deletion_requested user_id=%s email=%s", user.id, user.email)
    send_deletion_request_email(user.id, user.email)
    return {"success": True}


@auth_router.get("/me/payment-methods", response=list[PaymentMethodSchema], auth=JWTBearer())
def list_payment_methods(request):
    return list(request.auth.payment_methods.all())


@auth_router.put("/me/payment-methods/{provider}", response=PaymentMethodSchema, auth=JWTBearer())
def upsert_payment_method(request, provider: str, payload: PaymentMethodUpsertSchema):
    from ninjatab.auth.models import UserPaymentMethod
    if provider not in UserPaymentMethod.Provider.values:
        raise HttpError(400, "Unsupported provider")

    with transaction.atomic():
        pm, _ = UserPaymentMethod.objects.update_or_create(
            user=request.auth,
            provider=provider,
            defaults={"username": payload.username.strip()},
        )
        if payload.is_preferred:
            # At most one preferred method per user (enforced by a partial
            # unique constraint) — clear the others first.
            request.auth.payment_methods.exclude(pk=pm.pk).update(is_preferred=False)
        if pm.is_preferred != payload.is_preferred:
            pm.is_preferred = payload.is_preferred
            pm.save(update_fields=["is_preferred"])

    return pm


@auth_router.delete("/me/payment-methods/{provider}", response={204: None}, auth=JWTBearer())
def delete_payment_method(request, provider: str):
    request.auth.payment_methods.filter(provider=provider).delete()
    return 204, None
