"""Owner notifications: email (SMTP) and Telegram, both best-effort.

A channel that fails must never block the request; the pending approval is
always visible in the admin panel as a fallback.
"""
import html
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict, List

import requests

from .config import settings
from .tokens import make_decision_token


def _links(request_id: str) -> Dict[str, str]:
    base = settings.public_base_url.rstrip("/")
    return {
        "approve": f"{base}/auth/decide?token={make_decision_token(request_id, 'approve')}",
        "deny": f"{base}/auth/decide?token={make_decision_token(request_id, 'deny')}",
        "admin": f"{base}/admin",
    }


def _send_email(subject: str, text_body: str, html_body: str) -> bool:
    if not (settings.smtp_user and settings.smtp_password):
        print("[notify] SMTP no configurado; se omite el email.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = settings.owner_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - notification must not break auth
        print(f"[notify] Fallo enviando email: {exc}")
        return False


def _send_telegram(text: str, buttons: List[List[Dict[str, str]]]) -> bool:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] Fallo enviando Telegram: {exc}")
        return False


def notify_access_request(request_id: str, name: str, ip: str, user_agent: str) -> Dict[str, bool]:
    """Ask the owner to approve or reject a visitor. Returns per-channel status."""
    links = _links(request_id)
    minutes = settings.session_minutes
    safe_name = html.escape(name)
    safe_ua = html.escape(user_agent[:120])

    subject = f"[FactX] {name} pide acceso ({minutes} min)"
    text_body = (
        f"{name} quiere entrar en FactX Agent.\n\n"
        f"IP: {ip}\nNavegador: {user_agent[:120]}\n\n"
        f"AUTORIZAR {minutes} min: {links['approve']}\n"
        f"RECHAZAR:            {links['deny']}\n\n"
        f"Panel de control: {links['admin']}\n"
        f"Los enlaces caducan en {settings.request_ttl_minutes} minutos y solo sirven una vez."
    )
    html_body = f"""\
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1117;color:#e8eaf0;padding:24px">
  <div style="max-width:520px;margin:auto;background:#181b24;border:1px solid #2a2f3d;border-radius:14px;padding:28px">
    <h2 style="margin:0 0 6px">Solicitud de acceso</h2>
    <p style="margin:0 0 20px;color:#9aa3b8">Alguien quiere usar <strong>FactX Agent</strong>.</p>
    <table style="width:100%;font-size:14px;color:#c7cde0;border-collapse:collapse">
      <tr><td style="padding:6px 0;color:#9aa3b8">Nombre</td><td><strong>{safe_name}</strong></td></tr>
      <tr><td style="padding:6px 0;color:#9aa3b8">IP</td><td>{html.escape(ip)}</td></tr>
      <tr><td style="padding:6px 0;color:#9aa3b8">Navegador</td><td>{safe_ua}</td></tr>
    </table>
    <div style="margin:26px 0 8px">
      <a href="{links['approve']}" style="display:inline-block;background:#2ecc71;color:#06220f;text-decoration:none;font-weight:700;padding:13px 22px;border-radius:10px;margin-right:10px">Autorizar {minutes} min</a>
      <a href="{links['deny']}" style="display:inline-block;background:#e74c3c;color:#2a0b07;text-decoration:none;font-weight:700;padding:13px 22px;border-radius:10px">Rechazar</a>
    </div>
    <p style="color:#6e7689;font-size:12px;margin-top:22px">
      Caduca en {settings.request_ttl_minutes} min y solo funciona una vez.
      <a href="{links['admin']}" style="color:#7aa2ff">Abrir panel de control</a>
    </p>
  </div>
</body></html>"""

    telegram_text = (
        f"🔐 <b>{safe_name}</b> pide acceso a FactX\n"
        f"IP: <code>{html.escape(ip)}</code>\n"
        f"Sesión de {minutes} min si autorizas."
    )
    buttons = [[
        {"text": f"✅ Autorizar {minutes} min", "url": links["approve"]},
        {"text": "❌ Rechazar", "url": links["deny"]},
    ]]

    return {
        "email": _send_email(subject, text_body, html_body),
        "telegram": _send_telegram(telegram_text, buttons),
    }


def notify_owner_event(title: str, detail: str = "") -> None:
    """Low-priority heads-up (budget exhausted, kill switch flipped, ...)."""
    body = f"{title}\n\n{detail}" if detail else title
    _send_email(f"[FactX] {title}", body, f"<pre>{html.escape(body)}</pre>")
    _send_telegram(f"ℹ️ <b>{html.escape(title)}</b>\n{html.escape(detail)}", [])
