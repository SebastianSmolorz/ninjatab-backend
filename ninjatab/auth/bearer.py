import jwt
from ninja.security import HttpBearer
from django.contrib.auth.models import User

from ninjatab.auth.jwt_utils import decode_token


class JWTBearer(HttpBearer):
    def authenticate(self, request, token: str):
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                return None
            user = User.objects.get(id=payload["sub"])
            return user
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, User.DoesNotExist):
            return None
