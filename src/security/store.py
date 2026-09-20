"""SQLite-backed state for the access layer.

Everything the owner can toggle or revoke lives here rather than in memory, so
a container restart never resurrects a session that was killed, and the kill
switch survives redeploys.
"""
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_requests (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    ip         TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    session_id TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    ip         TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    runs_used  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS counters (
    day   TEXT NOT NULL,
    key   TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);
CREATE TABLE IF NOT EXISTS rate_limit (
    ip           TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON access_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(revoked, expires_at);
"""

WEB_ENABLED = "web_enabled"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def now() -> int:
    return int(time.time())


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        parent = os.path.dirname(os.path.abspath(settings.db_path))
        os.makedirs(parent, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def init() -> None:
    """Create the schema and apply the boot-time kill-switch default."""
    conn = connect()
    with _lock:
        row = conn.execute("SELECT value FROM flags WHERE key=?", (WEB_ENABLED,)).fetchone()
        if row is None:
            # Fail closed: an unattended restart leaves the site shut until the
            # owner explicitly turns it back on.
            conn.execute(
                "INSERT INTO flags(key, value) VALUES (?,?)",
                (WEB_ENABLED, "1" if settings.web_enabled_on_boot else "0"),
            )
        elif not settings.web_enabled_on_boot:
            conn.execute("UPDATE flags SET value='0' WHERE key=?", (WEB_ENABLED,))
        conn.commit()


# --- Kill switch -----------------------------------------------------------

def is_web_enabled() -> bool:
    conn = connect()
    row = conn.execute("SELECT value FROM flags WHERE key=?", (WEB_ENABLED,)).fetchone()
    return bool(row and row["value"] == "1")


def set_web_enabled(enabled: bool, actor: str = "admin") -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO flags(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (WEB_ENABLED, "1" if enabled else "0"),
        )
        conn.commit()
    log(actor, "web_on" if enabled else "web_off")


# --- Access requests -------------------------------------------------------

def create_request(name: str, ip: str, user_agent: str) -> str:
    rid = uuid.uuid4().hex
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO access_requests(id,name,ip,user_agent,status,created_at) VALUES (?,?,?,?, 'pending', ?)",
            (rid, name[:80], ip, user_agent[:200], now()),
        )
        conn.commit()
    return rid


def get_request(rid: str) -> Optional[sqlite3.Row]:
    return connect().execute("SELECT * FROM access_requests WHERE id=?", (rid,)).fetchone()


def pending_request_count() -> int:
    expire_stale_requests()
    row = connect().execute(
        "SELECT COUNT(*) AS c FROM access_requests WHERE status='pending'"
    ).fetchone()
    return int(row["c"])


def expire_stale_requests() -> None:
    cutoff = now() - settings.request_ttl_minutes * 60
    conn = connect()
    with _lock:
        conn.execute(
            "UPDATE access_requests SET status='expired', decided_at=? WHERE status='pending' AND created_at < ?",
            (now(), cutoff),
        )
        conn.commit()


def decide_request(rid: str, approve: bool, actor: str) -> Optional[str]:
    """Approve or deny a pending request. Returns the new session id on approve.

    Returns None when the request is missing or already decided, which is what
    makes an approval link single-use.
    """
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT * FROM access_requests WHERE id=? AND status='pending'", (rid,)
        ).fetchone()
        if row is None:
            return None
        if row["created_at"] < now() - settings.request_ttl_minutes * 60:
            conn.execute(
                "UPDATE access_requests SET status='expired', decided_at=? WHERE id=?", (now(), rid)
            )
            conn.commit()
            return None
        if not approve:
            conn.execute(
                "UPDATE access_requests SET status='denied', decided_at=? WHERE id=?", (now(), rid)
            )
            conn.commit()
            return None
        sid = uuid.uuid4().hex
        started = now()
        conn.execute(
            "INSERT INTO sessions(id,request_id,name,ip,created_at,expires_at) VALUES (?,?,?,?,?,?)",
            (sid, rid, row["name"], row["ip"], started, started + settings.session_minutes * 60),
        )
        conn.execute(
            "UPDATE access_requests SET status='approved', decided_at=?, session_id=? WHERE id=?",
            (started, sid, rid),
        )
        conn.commit()
    log(actor, "approve" if approve else "deny", rid)
    return sid


# --- Sessions --------------------------------------------------------------

def get_session(sid: str) -> Optional[sqlite3.Row]:
    return connect().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()


def session_is_live(sid: str) -> bool:
    row = get_session(sid)
    return bool(row and not row["revoked"] and row["expires_at"] > now())


def revoke_session(sid: str, actor: str = "admin") -> bool:
    conn = connect()
    with _lock:
        cur = conn.execute("UPDATE sessions SET revoked=1 WHERE id=? AND revoked=0", (sid,))
        conn.commit()
    if cur.rowcount:
        log(actor, "kill_session", sid)
        return True
    return False


def revoke_all_sessions(actor: str = "admin") -> int:
    conn = connect()
    with _lock:
        cur = conn.execute("UPDATE sessions SET revoked=1 WHERE revoked=0 AND expires_at > ?", (now(),))
        conn.commit()
    if cur.rowcount:
        log(actor, "kill_all_sessions", str(cur.rowcount))
    return cur.rowcount


def live_sessions() -> List[Dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM sessions WHERE revoked=0 AND expires_at > ? ORDER BY created_at DESC", (now(),)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "ip": r["ip"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "seconds_left": max(0, r["expires_at"] - now()),
            "runs_used": r["runs_used"],
        }
        for r in rows
    ]


def pending_requests() -> List[Dict[str, Any]]:
    expire_stale_requests()
    rows = connect().execute(
        "SELECT * FROM access_requests WHERE status='pending' ORDER BY created_at ASC"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "ip": r["ip"],
            "user_agent": r["user_agent"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def bump_session_runs(sid: str) -> int:
    conn = connect()
    with _lock:
        conn.execute("UPDATE sessions SET runs_used = runs_used + 1 WHERE id=?", (sid,))
        conn.commit()
    row = get_session(sid)
    return int(row["runs_used"]) if row else 0


# --- Daily counters --------------------------------------------------------

def counter_value(key: str) -> int:
    row = connect().execute(
        "SELECT value FROM counters WHERE day=? AND key=?", (_today(), key)
    ).fetchone()
    return int(row["value"]) if row else 0


def counter_add(key: str, amount: int = 1) -> int:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO counters(day,key,value) VALUES(?,?,?) "
            "ON CONFLICT(day,key) DO UPDATE SET value = value + excluded.value",
            (_today(), key, amount),
        )
        conn.commit()
    return counter_value(key)


def all_counters() -> Dict[str, int]:
    rows = connect().execute("SELECT key, value FROM counters WHERE day=?", (_today(),)).fetchall()
    return {r["key"]: int(r["value"]) for r in rows}


# --- Rate limiting ---------------------------------------------------------

def rate_limit_hit(ip: str, limit: int, window_seconds: int = 3600) -> bool:
    """Record a hit for `ip`. Returns True when the caller is over the limit."""
    conn = connect()
    with _lock:
        row = conn.execute("SELECT * FROM rate_limit WHERE ip=?", (ip,)).fetchone()
        current = now()
        if row is None or current - row["window_start"] >= window_seconds:
            conn.execute(
                "INSERT INTO rate_limit(ip,window_start,count) VALUES(?,?,1) "
                "ON CONFLICT(ip) DO UPDATE SET window_start=excluded.window_start, count=1",
                (ip, current),
            )
            conn.commit()
            return False
        if row["count"] >= limit:
            return True
        conn.execute("UPDATE rate_limit SET count = count + 1 WHERE ip=?", (ip,))
        conn.commit()
        return False


# --- Audit -----------------------------------------------------------------

def log(actor: str, action: str, detail: str = "") -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT INTO audit(at,actor,action,detail) VALUES (?,?,?,?)",
            (now(), actor, action, detail[:300]),
        )
        conn.commit()


def recent_audit(limit: int = 40) -> List[Dict[str, Any]]:
    rows = connect().execute(
        "SELECT at, actor, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
