"""Owner control panel: one place to see everything and pull any plug."""
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import budget, notify, store
from .config import settings
from .tokens import constant_time_equals

router = APIRouter(prefix="/admin", tags=["admin"])

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def require_admin(x_admin_token: str = Header(default="")) -> str:
    if not x_admin_token or not constant_time_equals(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="Token de administrador inválido.")
    return "owner"


class WebToggle(BaseModel):
    enabled: bool


class Decision(BaseModel):
    approve: bool


@router.get("", include_in_schema=False)
async def admin_page():
    """The panel itself holds no secrets; the API below is what is protected."""
    return FileResponse(os.path.join(_STATIC, "admin.html"))


@router.get("/api/state")
async def admin_state(actor: str = Depends(require_admin)):
    return {
        "web_enabled": store.is_web_enabled(),
        "session_minutes": settings.session_minutes,
        "sessions": store.live_sessions(),
        "pending": store.pending_requests(),
        "budgets": budget.snapshot(),
        "audit": store.recent_audit(25),
        "server_time": store.now(),
    }


@router.post("/api/web")
async def toggle_web(payload: WebToggle, actor: str = Depends(require_admin)):
    store.set_web_enabled(payload.enabled, actor=actor)
    killed = 0
    if not payload.enabled:
        # Switching the site off must also drop whoever is already inside.
        killed = store.revoke_all_sessions(actor=actor)
    return {"web_enabled": payload.enabled, "sessions_killed": killed}


@router.post("/api/requests/{request_id}/decide")
async def decide_request(request_id: str, payload: Decision, actor: str = Depends(require_admin)):
    sid = store.decide_request(request_id, approve=payload.approve, actor=actor)
    if payload.approve and sid is None:
        raise HTTPException(status_code=409, detail="La solicitud ya no estaba pendiente.")
    return {"ok": True, "session_id": sid}


@router.post("/api/sessions/kill-all")
async def kill_all(actor: str = Depends(require_admin)):
    return {"killed": store.revoke_all_sessions(actor=actor)}


@router.post("/api/sessions/{session_id}/kill")
async def kill_session(session_id: str, actor: str = Depends(require_admin)):
    if not store.revoke_session(session_id, actor=actor):
        raise HTTPException(status_code=404, detail="Sesión no encontrada o ya cerrada.")
    return {"ok": True}


@router.post("/api/panic")
async def panic(actor: str = Depends(require_admin)):
    """Big red button: site off and every session dropped, in one call."""
    killed = store.revoke_all_sessions(actor=actor)
    store.set_web_enabled(False, actor=actor)
    store.log(actor, "panic", f"sessions_killed={killed}")
    notify.notify_owner_event("Parada de emergencia", f"Sesiones cerradas: {killed}. Web apagada.")
    return {"web_enabled": False, "sessions_killed": killed}
