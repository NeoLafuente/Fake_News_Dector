"""The gate every request passes through.

Order of checks: kill switch first, then a live session. Anything that is not
explicitly public is closed.
"""
import json
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


def max_body_bytes() -> int:
    """The upload cap plus room for multipart framing."""
    return settings.max_upload_mb * 1024 * 1024 + 1024 * 1024


class _BodyTooLarge(Exception):
    """Raised from the receive channel once the cap is crossed."""


class BodySizeLimitMiddleware:
    """Cap request bodies at the ASGI layer, as they arrive.

    Checking Content-Length is not enough on its own: a chunked request, a
    missing header or a misreported one reaches Starlette's multipart parser,
    which buffers the entire body before any handler can object. Counting bytes
    off the receive channel is what turns MAX_UPLOAD_MB into a real limit, and
    it stops the transfer instead of discovering the problem afterwards.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send) -> None:
        body = json.dumps({
            "detail": f"El archivo supera el máximo de {settings.max_upload_mb} MB.",
            "code": "payload_too_large",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Cheap path first: refuse an honestly declared oversize body without
        # reading a single byte of it.
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        return await self._reject(send)
                except ValueError:
                    pass  # Unparseable: fall through to counting.
                break

        received = 0
        too_large = False
        forwarded_start = False
        replaced = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal forwarded_start, replaced
            if too_large:
                # Starlette's multipart parser catches the abort and answers
                # 400 "error parsing the body". The transfer is stopped either
                # way, but the caller deserves the accurate status, so the
                # downstream response is swallowed and replaced.
                if not replaced and not forwarded_start:
                    replaced = True
                    await self._reject(send)
                return
            if message["type"] == "http.response.start":
                forwarded_start = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            # Nothing downstream caught it, so nothing has answered yet.
            if not replaced and not forwarded_start:
                await self._reject(send)


def _no_store(response):
    """Forbid caching of everything the gate serves.

    /auth/status is polled every few seconds at an identical URL, and / returns
    either the gate or the app depending on the session. A browser or a CDN is
    entitled to cache a plain GET, and caching either one strands the visitor:
    the poll keeps replaying a stale "pending" while the session is already
    live on the server.

    This covers the static assets too. They are small, and never serving a
    stale app.js is worth more here than the saved bytes.
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def install(app) -> None:
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes())

    @app.middleware("http")
    async def access_gate(request: Request, call_next):
        path = request.url.path

        # The admin surface stays reachable even when the site is switched off,
        # otherwise the owner could never switch it back on.
        if path == "/admin" or path.startswith("/admin/"):
            return _no_store(await call_next(request))

        if not store.is_web_enabled():
            if _is_public(path):
                return _no_store(await call_next(request))
            return _no_store(JSONResponse(
                {"detail": "La web está apagada por el administrador.", "code": "web_disabled"},
                status_code=503,
            ))

        if _is_public(path):
            return _no_store(await call_next(request))

        if current_session_id(request) is None:
            return _no_store(JSONResponse(
                {
                    "detail": "Necesitas una sesión autorizada.",
                    "code": "no_session",
                    "session_minutes": settings.session_minutes,
                },
                status_code=401,
            ))

        return _no_store(await call_next(request))
