import jwt
from ninja import Router
from ninja.errors import HttpError
from django.contrib.auth.models import User

from ninjatab.auth.schemas import (
    MagicLinkSchema,
    MagicLinkSuccessSchema,
    VerifyMagicLinkSchema,
    TokenResponseSchema,
    AuthUserSchema,
    RefreshSchema,
    RefreshResponseSchema,
)
from ninjatab.auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    create_magic_token,
    decode_token,
)
from ninjatab.auth.bearer import JWTBearer
from ninjatab.auth.email import send_magic_link

auth_router = Router(tags=["auth"])


@auth_router.post("/magic-link", response=MagicLinkSuccessSchema)
def magic_link(request, payload: MagicLinkSchema):
    user, _ = User.objects.get_or_create(
        email=payload.email,
        defaults={"username": payload.email},
    )
    token = create_magic_token(user.id)
    send_magic_link(payload.email, token)
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

    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": user,
    }


@auth_router.post("/refresh", response=RefreshResponseSchema)
def refresh(request, payload: RefreshSchema):
    try:
        token_data = decode_token(payload.refresh_token)
        if token_data.get("type") != "refresh":
            raise HttpError(401, "Invalid token type")
        user = User.objects.get(id=int(token_data["sub"]))
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise HttpError(401, "Invalid or expired refresh token")
    except User.DoesNotExist:
        raise HttpError(401, "User not found")

    new_access_token = create_access_token(user.id, user.email)
    return {"access_token": new_access_token}


@auth_router.get("/me", response=AuthUserSchema, auth=JWTBearer())
def me(request):
    return request.auth
