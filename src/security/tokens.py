"""Signed, expiring, single-use-by-construction tokens.

Approval links travel through email and Telegram, so they must be unguessable
and must stop working on their own. Single use is enforced in the store: a
request leaves 'pending' the first time a link is followed.
"""
import hashlib
import hmac
from typing import Optional, Tuple

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

_DECISION_SALT = "factx.decision.v1"
_COOKIE_SALT = "factx.session.v1"

SESSION_COOKIE = "factx_session"


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


# --- Approve / deny links --------------------------------------------------

def make_decision_token(request_id: str, action: str) -> str:
    return _serializer(_DECISION_SALT).dumps({"rid": request_id, "action": action})


def read_decision_token(token: str) -> Optional[Tuple[str, str]]:
    """Return (request_id, action) or None when invalid/expired."""
    try:
        data = _serializer(_DECISION_SALT).loads(
            token, max_age=settings.request_ttl_minutes * 60
        )
    except (BadSignature, SignatureExpired):
        return None
    rid, action = data.get("rid"), data.get("action")
    if not rid or action not in {"approve", "deny"}:
        return None
    return rid, action


# --- Session cookie --------------------------------------------------------

def make_session_cookie(session_id: str) -> str:
    return _serializer(_COOKIE_SALT).dumps({"sid": session_id})


def read_session_cookie(value: str) -> Optional[str]:
    """Return the session id carried by a valid cookie, else None.

    The cookie only proves the id was issued by us; whether that session is
    still alive is decided by the store, so revocation is immediate.
    """
    try:
        data = _serializer(_COOKIE_SALT).loads(
            value, max_age=settings.session_minutes * 60 + 60
        )
    except (BadSignature, SignatureExpired):
        return None
    sid = data.get("sid")
    return sid if isinstance(sid, str) else None


# --- Shared secrets --------------------------------------------------------

def constant_time_equals(a: str, b: str) -> bool:
    """Compare secrets without leaking their length through timing."""
    return hmac.compare_digest(
        hashlib.sha256(a.encode("utf-8")).digest(),
        hashlib.sha256(b.encode("utf-8")).digest(),
    )
