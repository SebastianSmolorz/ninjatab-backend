import jwt
from ninja.security import HttpBearer
from django.contrib.auth import get_user_model

from ninjatab.auth.jwt_utils import decode_token


class JWTBearer(HttpBearer):
    def authenticate(self, request, token: str):
        User = get_user_model()
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                return None
            user = User.objects.get(id=int(payload["sub"]))
            return user
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, User.DoesNotExist):
            return None
