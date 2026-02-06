import jwt
from ninja import Router
from ninja.errors import HttpError
from django.contrib.auth.models import User

from ninjatab.auth.schemas import (
    LoginSchema,
    TokenResponseSchema,
    AuthUserSchema,
    RefreshSchema,
    RefreshResponseSchema,
)
from ninjatab.auth.jwt_utils import create_access_token, create_refresh_token, decode_token
from ninjatab.auth.bearer import JWTBearer

auth_router = Router(tags=["auth"])


@auth_router.post("/login", response=TokenResponseSchema)
def login(request, payload: LoginSchema):
    try:
        user = User.objects.get(email=payload.email)
    except User.DoesNotExist:
        raise HttpError(401, "No user found with this email")

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
