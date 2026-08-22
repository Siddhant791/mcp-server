from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
import json as _json
from urllib.parse import urlencode

import jwt
from jwt import PyJWKClient

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip().rstrip("/").removeprefix("https://").removeprefix("http://")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

_jwk_client: PyJWKClient | None = None


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(GOOGLE_JWKS_URL, cache_jwk_set=True, cache_keys=True)
    return _jwk_client


def get_google_auth_url(state: str, redirect_uri: str = "") -> str:
    uri = redirect_uri or GOOGLE_REDIRECT_URI
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str, client_id: str | None = None, client_secret: str | None = None, redirect_uri: str = "") -> dict:
    cid = client_id or GOOGLE_CLIENT_ID
    csecret = client_secret or GOOGLE_CLIENT_SECRET
    uri = redirect_uri or GOOGLE_REDIRECT_URI
    data = urlencode({
        "code": code,
        "client_id": cid,
        "client_secret": csecret,
        "redirect_uri": uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Google token exchange failed ({e.code}): {body}")


async def get_user_info_from_google(access_token: str) -> dict:
    req = urllib.request.Request(GOOGLE_USERINFO_URL)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Google userinfo fetch failed ({e.code}): {body}")


def verify_google_token(token: str) -> dict | None:
    """Verify a Google-issued ID token or access token using Google's JWKS."""
    try:
        jwk_client = _get_jwk_client()
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        return payload
    except Exception:
        return None


def verify_token(token: str) -> dict | None:
    """Verify a token — try Google JWT first, then fall back to server-issued JWT."""
    if not token:
        return None

    google_payload = verify_google_token(token)
    if google_payload:
        return {
            "sub": google_payload.get("sub", ""),
            "email": google_payload.get("email", ""),
            "name": google_payload.get("name", ""),
            "_source": "google",
        }

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def create_jwt(user_id: str, email: str, name: str, role: str, master_user_id: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "name": name,
        "role": role,
        "master_user_id": master_user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + (JWT_EXPIRY_HOURS * 3600),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_jwt(token: str) -> dict | None:
    return verify_token(token)
