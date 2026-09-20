"""The gate every request passes through.

Order of checks: kill switch first, then a live session. Anything that is not
explicitly public is closed.
"""
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from . import store
from .config import settings
from .tokens import SESSION_COOKIE, read_session_cookie

# Reachable with no session at all. Everything else — including /docs and
# /openapi.json — requires an approved session.
PUBLIC_EXACT = {"/", "/health", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/", "/auth/")


def client_ip(request: Request) -> str:
    """The IP the rate limiter keys on.

    Forwarding headers are only honoured when TRUST_PROXY_HEADERS says the
    origin sits behind a proxy that sets them. Trusting them unconditionally
    would let anyone who can reach the origin rotate the header and walk
    straight past AUTH_REQUESTS_PER_HOUR.
    """
    if settings.trust_proxy_headers:
        for header in ("cf-connecting-ip", "x-forwarded-for"):
            value = request.headers.get(header)
            if value:
                return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_session_id(request: Request) -> Optional[str]:
    """Session id from the cookie, only if that session is still alive."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    sid = read_session_cookie(raw)
    if not sid or not store.session_is_live(sid):
        return None
    return sid


def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def install(app) -> None:
    # Multipart bodies are buffered by Starlette before any handler runs, so an
    # oversized upload has to be refused from the declared length first; the
    # streaming check in save_upload still covers clients that lie or chunk.
    max_body_bytes = settings.max_upload_mb * 1024 * 1024 + 1024 * 1024

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_body_bytes:
            return JSONResponse(
                {
                    "detail": f"El archivo supera el máximo de {settings.max_upload_mb} MB.",
                    "code": "payload_too_large",
                },
                status_code=413,
            )

        # The admin surface stays reachable even when the site is switched off,
        # otherwise the owner could never switch it back on.
        if path == "/admin" or path.startswith("/admin/"):
            return await call_next(request)

        if not store.is_web_enabled():
            if _is_public(path):
                return await call_next(request)
            return JSONResponse(
                {"detail": "La web está apagada por el administrador.", "code": "web_disabled"},
                status_code=503,
            )

        if _is_public(path):
            return await call_next(request)

        if current_session_id(request) is None:
            return JSONResponse(
                {
                    "detail": "Necesitas una sesión autorizada.",
                    "code": "no_session",
                    "session_minutes": settings.session_minutes,
                },
                status_code=401,
            )

        return await call_next(request)
