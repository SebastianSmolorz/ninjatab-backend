import time
import logging

import jwt
import requests
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from django.conf import settings

logger = logging.getLogger("app")

# Cache Apple's public keys for 24 hours
_apple_keys_cache = {"keys": None, "fetched_at": 0}


def verify_google_id_token(token_str: str) -> dict:
    """Verify a Google ID token and return user info.

    Raises ValueError on any verification failure.
    """
    idinfo = google_id_token.verify_oauth2_token(
        token_str,
        google_requests.Request(),
    )

    if idinfo["aud"] not in settings.GOOGLE_OAUTH_CLIENT_IDS:
        logger.error("Google aud mismatch: token_aud=%s, allowed=%s", idinfo["aud"], settings.GOOGLE_OAUTH_CLIENT_IDS)
        raise ValueError("Invalid audience")

    if not idinfo.get("email_verified", False):
        raise ValueError("Email not verified")

    return {
        "email": idinfo["email"],
        "first_name": idinfo.get("given_name", ""),
        "last_name": idinfo.get("family_name", ""),
    }


def _get_apple_public_keys():
    now = time.time()
    if _apple_keys_cache["keys"] and (now - _apple_keys_cache["fetched_at"]) < 86400:
        return _apple_keys_cache["keys"]

    resp = requests.get("https://appleid.apple.com/auth/keys", timeout=10)
    resp.raise_for_status()
    jwks = resp.json()
    _apple_keys_cache["keys"] = jwks["keys"]
    _apple_keys_cache["fetched_at"] = now
    return _apple_keys_cache["keys"]


def verify_apple_id_token(token_str: str) -> dict:
    """Verify an Apple ID token and return user info.

    Raises ValueError on any verification failure.
    """
    header = jwt.get_unverified_header(token_str)
    kid = header.get("kid")
    if not kid:
        raise ValueError("Missing kid in token header")

    apple_keys = _get_apple_public_keys()
    matching_key = next((k for k in apple_keys if k["kid"] == kid), None)
    if not matching_key:
        raise ValueError("No matching Apple public key")

    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(matching_key)

    payload = jwt.decode(
        token_str,
        public_key,
        algorithms=["RS256"],
        audience=settings.APPLE_SIGN_IN_AUDIENCE,
        issuer="https://appleid.apple.com",
    )

    email = payload.get("email")
    if not email:
        raise ValueError("No email in Apple token")

    return {
        "email": email,
        "first_name": "",
        "last_name": "",
    }
