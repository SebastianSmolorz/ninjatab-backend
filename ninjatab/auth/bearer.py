import jwt
from ninja.security import HttpBearer
from django.contrib.auth import get_user_model

from ninjatab.auth.cookies import ACCESS_COOKIE
from ninjatab.auth.jwt_utils import decode_token


class JWTBearer(HttpBearer):
    def __call__(self, request):
        # Check cookie first
        token = request.COOKIES.get(ACCESS_COOKIE)
        if token:
            return self.authenticate(request, token)
        # Fall back to Authorization header (keeps Swagger working)
        return super().__call__(request)

    def authenticate(self, request, token):
        User = get_user_model()
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                return None
            user = User.objects.get(id=int(payload["sub"]))
            return user
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, User.DoesNotExist):
            return None
