"""Visitor-facing authentication: password, owner approval, 10-minute session."""
import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import notify, store
from .config import settings
from .middleware import client_ip, current_session_id
from .tokens import SESSION_COOKIE, make_session_cookie, read_decision_token, constant_time_equals

router = APIRouter(prefix="/auth", tags=["auth"])


class AccessRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    name: str = Field(default="Invitado", max_length=80)


def _decision_page(title: str, message: str, tone: str) -> HTMLResponse:
    colors = {"ok": "#2ecc71", "bad": "#e74c3c", "warn": "#f1c40f"}
    accent = colors.get(tone, "#7aa2ff")
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1117;
             color:#e8eaf0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:24px">
  <div style="max-width:420px;text-align:center;background:#181b24;border:1px solid #2a2f3d;
              border-radius:16px;padding:36px">
    <div style="font-size:44px;line-height:1;margin-bottom:14px">
      {'✅' if tone == 'ok' else '⛔' if tone == 'bad' else '⏳'}
    </div>
    <h1 style="margin:0 0 10px;font-size:21px;color:{accent}">{html.escape(title)}</h1>
    <p style="margin:0;color:#9aa3b8;line-height:1.6">{html.escape(message)}</p>
    <a href="/admin" style="display:inline-block;margin-top:24px;color:#7aa2ff;font-size:14px">
      Abrir panel de control
    </a>
  </div>
</body></html>""")


def _confirm_page(token: str, action: str, name: str, ip: str) -> HTMLResponse:
    """The one click that actually decides. Rendered by a GET, submitted by POST."""
    approving = action == "approve"
    accent = "#2ecc71" if approving else "#e74c3c"
    verb = f"Autorizar {settings.session_minutes} min" if approving else "Rechazar"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Confirmar decisión</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1117;
             color:#e8eaf0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:24px">
  <div style="max-width:420px;width:100%;text-align:center;background:#181b24;
              border:1px solid #2a2f3d;border-radius:16px;padding:34px">
    <div style="font-size:40px;margin-bottom:12px">{'✅' if approving else '⛔'}</div>
    <h1 style="margin:0 0 8px;font-size:20px">Confirma la decisión</h1>
    <p style="margin:0 0 6px;color:#9aa3b8;line-height:1.6">
      <strong style="color:#fff">{html.escape(name)}</strong> pide acceso
    </p>
    <p style="margin:0 0 24px;color:#6e7689;font-size:13px">IP {html.escape(ip)}</p>
    <form method="post" action="/auth/decide">
      <input type="hidden" name="token" value="{html.escape(token)}">
      <input type="hidden" name="action" value="{html.escape(action)}">
      <button type="submit" style="width:100%;padding:15px;border:0;border-radius:11px;
              background:{accent};color:#06220f;font-family:inherit;font-size:1rem;
              font-weight:700;cursor:pointer">{verb}</button>
    </form>
    <a href="/admin" style="display:inline-block;margin-top:20px;color:#7aa2ff;font-size:13px">
      Abrir panel de control
    </a>
  </div>
</body></html>""")


@router.get("/state")
async def auth_state(request: Request):
    """What the login screen needs in order to render the right thing."""
    sid = current_session_id(request)
    seconds_left = 0
    if sid:
        row = store.get_session(sid)
        if row:
            seconds_left = max(0, row["expires_at"] - store.now())
    return {
        "web_enabled": store.is_web_enabled(),
        "has_session": sid is not None,
        "seconds_left": seconds_left,
        "session_minutes": settings.session_minutes,
    }


@router.post("/request")
def request_access(payload: AccessRequest, request: Request):  # sync: threadpool
    """Step 1: the visitor proves they know the shared password, then waits.

    Declared sync on purpose. It calls blocking SMTP and Telegram clients with
    multi-second timeouts, and on the event loop a slow mail server would stall
    every other request, the admin panel included.
    """
    if not store.is_web_enabled():
        return JSONResponse(
            {"detail": "La web está apagada.", "code": "web_disabled"}, status_code=503
        )

    ip = client_ip(request)
    if store.rate_limit_hit(ip, settings.auth_requests_per_hour):
        store.log(ip, "rate_limited")
        return JSONResponse(
            {"detail": "Demasiados intentos. Prueba dentro de una hora.", "code": "rate_limited"},
            status_code=429,
        )

    if not constant_time_equals(payload.password, settings.gate_password):
        store.log(ip, "bad_password")
        return JSONResponse(
            {"detail": "Credencial incorrecta.", "code": "bad_password"}, status_code=403
        )

    if store.pending_request_count() >= settings.max_pending_requests:
        return JSONResponse(
            {"detail": "Hay demasiadas solicitudes en cola.", "code": "queue_full"},
            status_code=429,
        )

    name = (payload.name or "Invitado").strip() or "Invitado"
    rid = store.create_request(name, ip, request.headers.get("user-agent", ""))
    channels = notify.notify_access_request(rid, name, ip, request.headers.get("user-agent", ""))
    store.log(ip, "access_requested", f"{name} -> {channels}")

    return {
        "request_id": rid,
        "status": "pending",
        "notified": channels,
        "expires_in": settings.request_ttl_minutes * 60,
    }


@router.get("/status")
async def request_status(request_id: str):
    """Step 2: the visitor's browser polls here; approval lands the cookie."""
    row = store.get_request(request_id)
    if row is None:
        return JSONResponse({"detail": "Solicitud desconocida.", "status": "unknown"}, status_code=404)

    status = row["status"]
    if status != "approved":
        return {"status": status}

    sid = row["session_id"]
    if not sid or not store.session_is_live(sid):
        return {"status": "expired"}

    session = store.get_session(sid)
    seconds_left = max(0, session["expires_at"] - store.now())
    response = JSONResponse({"status": "approved", "seconds_left": seconds_left})
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(sid),
        max_age=seconds_left,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/decide")
async def decide_preview(token: str):
    """Step 3a: show what is being decided. Deliberately does not change state.

    Mail scanners, link previews and Telegram's own fetcher follow links before
    a human ever clicks. If this GET mutated, one of them could silently approve
    or deny a request, so the actual decision is a POST from the page below.
    """
    parsed = read_decision_token(token)
    if parsed is None:
        return _decision_page(
            "Enlace caducado", "Este enlace ya no es válido. Pide al usuario que lo intente otra vez.", "warn"
        )

    rid, action = parsed
    row = store.get_request(rid)
    if row is None:
        return _decision_page("Solicitud desconocida", "No existe esa solicitud.", "warn")
    if row["status"] != "pending":
        return _decision_page(
            "Ya decidido",
            f"Esta solicitud de {row['name']} ya estaba marcada como '{row['status']}'.",
            "warn",
        )

    return _confirm_page(token, action, row["name"], row["ip"])


@router.post("/decide")
async def decide(token: str = Form(...), action: str = Form(...)):
    """Step 3b: the owner confirms, and only now does the state change."""
    parsed = read_decision_token(token)
    if parsed is None:
        return _decision_page(
            "Enlace caducado", "Este enlace ya no es válido. Pide al usuario que lo intente otra vez.", "warn"
        )

    rid, token_action = parsed
    # The signed token decides, not the submitted field.
    if action != token_action:
        return _decision_page("Petición inconsistente", "Vuelve a abrir el enlace.", "warn")

    row = store.get_request(rid)
    if row is None:
        return _decision_page("Solicitud desconocida", "No existe esa solicitud.", "warn")
    if row["status"] != "pending":
        return _decision_page(
            "Ya decidido",
            f"Esta solicitud de {row['name']} ya estaba marcada como '{row['status']}'.",
            "warn",
        )

    sid = store.decide_request(rid, approve=(token_action == "approve"), actor="owner")
    if token_action == "approve":
        if sid is None:
            return _decision_page("Caducada", "La solicitud expiró antes de que la aprobaras.", "warn")
        return _decision_page(
            "Acceso autorizado",
            f"{row['name']} tiene {settings.session_minutes} minutos de sesión. "
            "Puedes cortarla en cualquier momento desde el panel.",
            "ok",
        )
    return _decision_page("Acceso rechazado", f"{row['name']} no podrá entrar.", "bad")


@router.post("/logout")
async def logout(request: Request):
    sid = current_session_id(request)
    if sid:
        store.revoke_session(sid, actor="user")
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
