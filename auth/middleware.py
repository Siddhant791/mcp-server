from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth.oauth import verify_token
from auth.models import AuthContext

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.types import ASGIApp

user_context: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar("user_context", default=None)


def get_current_user() -> AuthContext | None:
    return user_context.get()


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path.startswith("/.well-known") or path in ("/authorize", "/auth/callback", "/register", "/token"):
            tok = user_context.set(None)
            try:
                return await call_next(request)
            finally:
                user_context.reset(tok)

        auth_header = request.headers.get("authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        if token:
            payload = verify_token(token)
            if payload:
                if payload.get("_source") == "google":
                    ctx = AuthContext(
                        user_id=payload["sub"],
                        email=payload.get("email", ""),
                        name=payload.get("name", ""),
                        role="master",
                        master_user_id=payload["sub"],
                    )
                else:
                    ctx = AuthContext(
                        user_id=payload["sub"],
                        email=payload.get("email", ""),
                        name=payload.get("name", ""),
                        role=payload["role"],
                        master_user_id=payload["master_user_id"],
                    )
                tok = user_context.set(ctx)
                try:
                    response = await call_next(request)
                    return response
                finally:
                    user_context.reset(tok)
            else:
                return JSONResponse(
                    {"error": "Invalid or expired token. Please re-authenticate."},
                    status_code=401,
                )

        return JSONResponse(
            {"error": "Authentication required. Please authenticate via OAuth first."},
            status_code=401,
        )


def require_auth() -> AuthContext:
    ctx = get_current_user()
    if ctx is None:
        raise PermissionError("Authentication required. Please authenticate via OAuth first.")
    return ctx


def require_master() -> AuthContext:
    ctx = require_auth()
    if ctx.role != "master":
        raise PermissionError("This action requires master privileges.")
    return ctx


def check_family_permission(permission: str) -> AuthContext:
    ctx = require_auth()
    if ctx.role == "master":
        return ctx
    raise PermissionError(f"This action requires the '{permission}' permission.")
