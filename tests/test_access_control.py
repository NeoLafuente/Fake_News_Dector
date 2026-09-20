"""Regression tests for the access layer.

Deliberately free of the heavy agent dependencies: src.app imports the LangGraph
and transcription stacks lazily, so the whole gate can be exercised with nothing
more than fastapi + itsdangerous installed.

    pip install pytest httpx
    pytest tests/ -v
"""
import os
import tempfile

import pytest

DB_DIR = tempfile.mkdtemp()
os.environ.setdefault("SECRET_KEY", "t" * 64)
os.environ.setdefault("GATE_PASSWORD", "clave-de-prueba")
os.environ.setdefault("ADMIN_TOKEN", "token-de-prueba")
os.environ.setdefault("SECURITY_DB_PATH", os.path.join(DB_DIR, "test.db"))
os.environ.setdefault("WEB_ENABLED_ON_BOOT", "false")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("MAX_RUNS_PER_SESSION", "2")
os.environ.setdefault("AUTH_REQUESTS_PER_HOUR", "50")

from fastapi.testclient import TestClient  # noqa: E402

from src.app import app  # noqa: E402
from src.security import budget, store  # noqa: E402
from src.security.tokens import make_decision_token  # noqa: E402

ADMIN = {"X-Admin-Token": "token-de-prueba"}


@pytest.fixture
def client():
    """A fresh client against a reset store, with the site switched off."""
    conn = store.connect()
    for table in ("sessions", "access_requests", "counters", "rate_limit", "audit"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    store.set_web_enabled(False)
    with TestClient(app) as test_client:
        yield test_client


def decide(client, request_id, action):
    """Follow an approval link the way the owner does: preview, then confirm."""
    token = make_decision_token(request_id, action)
    client.get(f"/auth/decide?token={token}")
    return client.post("/auth/decide", data={"token": token, "action": action})


def approve(client, name="Invitado", password="clave-de-prueba"):
    """Walk a visitor all the way to an authorised session."""
    request_id = client.post("/auth/request", json={"password": password, "name": name}).json()["request_id"]
    decide(client, request_id, "approve")
    client.get(f"/auth/status?request_id={request_id}")
    return request_id


# --- Kill switch -----------------------------------------------------------

def test_boots_with_the_site_switched_off(client):
    assert store.is_web_enabled() is False
    assert client.post("/analyze_text", json={"transcript": "x"}).status_code == 503


def test_health_stays_reachable_while_off(client):
    assert client.get("/health").status_code == 200


def test_admin_panel_reachable_while_off(client):
    # Otherwise the owner could never switch the site back on.
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/api/state", headers=ADMIN).status_code == 200


def test_root_serves_the_gate_when_off(client):
    assert b"Solicitar acceso" in client.get("/").content


# --- Admin authentication --------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "incorrecto"}])
def test_admin_api_rejects_bad_tokens(client, headers):
    assert client.get("/admin/api/state", headers=headers).status_code == 401


# --- Shared credential -----------------------------------------------------

def test_wrong_password_is_refused(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    assert client.post("/auth/request", json={"password": "mala", "name": "Eva"}).status_code == 403


def test_session_is_required_even_when_the_site_is_on(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    assert client.post("/analyze_text", json={"transcript": "x"}).status_code == 401
    # The API surface must not be enumerable without a session either.
    assert client.get("/docs").status_code == 401


def test_rate_limit_protects_the_owner_inbox(client):
    os.environ["AUTH_REQUESTS_PER_HOUR"] = "50"
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    from src.security.config import settings

    codes = [
        client.post("/auth/request", json={"password": "mala", "name": "bot"}).status_code
        for _ in range(settings.auth_requests_per_hour + 3)
    ]
    assert 429 in codes


# --- Owner approval --------------------------------------------------------

def test_request_stays_pending_until_the_owner_decides(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = client.post(
        "/auth/request", json={"password": "clave-de-prueba", "name": "Eva"}
    ).json()["request_id"]
    assert client.get(f"/auth/status?request_id={request_id}").json()["status"] == "pending"
    assert client.post("/analyze_text", json={"transcript": "x"}).status_code == 401


def test_tampered_approval_link_is_rejected(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = client.post(
        "/auth/request", json={"password": "clave-de-prueba", "name": "Eva"}
    ).json()["request_id"]
    forged = make_decision_token(request_id, "approve")[:-4] + "xxxx"
    assert "Enlace caducado" in client.get(f"/auth/decide?token={forged}").text
    assert "Enlace caducado" in client.post(
        "/auth/decide", data={"token": forged, "action": "approve"}
    ).text


def test_approval_link_works_exactly_once(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = client.post(
        "/auth/request", json={"password": "clave-de-prueba", "name": "Eva"}
    ).json()["request_id"]
    token = make_decision_token(request_id, "approve")
    first = client.post("/auth/decide", data={"token": token, "action": "approve"})
    assert "Acceso autorizado" in first.text
    second = client.post("/auth/decide", data={"token": token, "action": "approve"})
    assert "Ya decidido" in second.text


def test_denied_request_grants_nothing(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = client.post(
        "/auth/request", json={"password": "clave-de-prueba", "name": "Eva"}
    ).json()["request_id"]
    decide(client, request_id, "deny")
    assert client.get(f"/auth/status?request_id={request_id}").json()["status"] == "denied"
    assert client.post("/analyze_text", json={"transcript": "x"}).status_code == 401


# --- The ten-minute session ------------------------------------------------

def test_approved_session_lasts_the_configured_window(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = approve(client, "Eva")
    body = client.get(f"/auth/status?request_id={request_id}").json()
    assert body["status"] == "approved"
    assert 590 <= body["seconds_left"] <= 600
    assert "factx_session" in client.cookies
    assert b"Solicitar acceso" not in client.get("/").content


def test_revoking_a_session_takes_effect_immediately(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    approve(client, "Eva")
    session_id = store.live_sessions()[0]["id"]
    client.post(f"/admin/api/sessions/{session_id}/kill", headers=ADMIN)
    assert client.post("/analyze_text", json={"transcript": "x"}).status_code == 401


def test_switching_the_site_off_drops_live_sessions(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    approve(client, "Eva")
    result = client.post("/admin/api/web", headers=ADMIN, json={"enabled": False}).json()
    assert result["sessions_killed"] == 1
    assert store.live_sessions() == []


def test_panic_button_clears_everything(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    approve(client, "Eva")
    result = client.post("/admin/api/panic", headers=ADMIN).json()
    assert result["sessions_killed"] == 1
    assert store.is_web_enabled() is False
    assert client.post("/analyze_text", json={"transcript": "x"}).status_code == 503


# --- Cost ceilings ---------------------------------------------------------

def test_runs_per_session_are_capped(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    approve(client, "Eva")
    # MAX_RUNS_PER_SESSION=2: the first two clear the gate (and then fail for
    # want of API keys), the third is refused outright.
    for _ in range(2):
        assert client.post("/analyze_text", json={"transcript": "t"}).status_code != 429
    assert client.post("/analyze_text", json={"transcript": "t"}).status_code == 429


def test_daily_budget_raises_once_exhausted(client):
    from src.security.config import settings

    budget.claim(budget.LLM_CALLS, settings.daily_llm_call_budget)
    with pytest.raises(budget.BudgetExceeded):
        budget.claim(budget.LLM_CALLS)


def test_daily_counters_survive_a_restart(client):
    budget.claim(budget.SEARCHES, 5)
    store._conn = None  # simulate the process coming back up
    assert store.counter_value(budget.SEARCHES) == 5


def test_transcript_is_truncated_before_reaching_the_llm(client):
    from src.security.config import settings

    clamped = budget.clamp_transcript("palabra " * 10000)
    assert len(clamped) < settings.max_transcript_chars + 100
    assert "truncado" in clamped


# --- Upload hardening ------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "../../../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "/etc/shadow",
])
def test_upload_filenames_cannot_escape_the_download_directory(hostile):
    from src.data_ingestion_transcription.audio_extractor import safe_filename

    result = safe_filename(hostile)
    assert "/" not in result and "\\" not in result and ".." not in result


# --- Approval links are safe to prefetch -----------------------------------

def test_get_on_a_decision_link_does_not_decide(client):
    """Mail scanners and link previews follow links; that must change nothing."""
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = client.post(
        "/auth/request", json={"password": "clave-de-prueba", "name": "Eva"}
    ).json()["request_id"]
    token = make_decision_token(request_id, "approve")

    preview = client.get(f"/auth/decide?token={token}")
    assert preview.status_code == 200
    assert "Confirma la decisión" in preview.text
    assert client.get(f"/auth/status?request_id={request_id}").json()["status"] == "pending"

    client.post("/auth/decide", data={"token": token, "action": "approve"})
    assert client.get(f"/auth/status?request_id={request_id}").json()["status"] == "approved"


def test_submitted_action_cannot_override_the_signed_one(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    request_id = client.post(
        "/auth/request", json={"password": "clave-de-prueba", "name": "Eva"}
    ).json()["request_id"]
    deny_token = make_decision_token(request_id, "deny")
    response = client.post("/auth/decide", data={"token": deny_token, "action": "approve"})
    assert "Petición inconsistente" in response.text
    assert client.get(f"/auth/status?request_id={request_id}").json()["status"] == "pending"


# --- Quota accounting ------------------------------------------------------

def test_a_refused_run_does_not_burn_quota(client):
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    approve(client, "Eva")
    session_id = store.live_sessions()[0]["id"]

    for _ in range(2):
        client.post("/analyze_text", json={"transcript": "t"})
    for _ in range(3):
        assert client.post("/analyze_text", json={"transcript": "t"}).status_code == 429

    # Rejections must not keep incrementing the stored counter past the limit.
    assert store.get_session(session_id)["runs_used"] == 2


def test_budget_claim_is_all_or_nothing(client):
    from src.security.config import settings

    limit = settings.daily_llm_call_budget
    budget.claim(budget.LLM_CALLS, limit - 1)
    # A claim that does not fit must leave the counter untouched.
    with pytest.raises(budget.BudgetExceeded):
        budget.claim(budget.LLM_CALLS, 5)
    assert store.counter_value(budget.LLM_CALLS) == limit - 1
    budget.claim(budget.LLM_CALLS, 1)
    assert store.counter_value(budget.LLM_CALLS) == limit


def test_concurrent_claims_cannot_oversubscribe(client):
    import threading

    from src.security.config import settings

    granted = []

    def worker():
        try:
            budget.claim(budget.SEARCHES)
            granted.append(1)
        except budget.BudgetExceeded:
            pass

    limit = settings.daily_search_budget
    threads = [threading.Thread(target=worker) for _ in range(limit + 20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == limit
    assert store.counter_value(budget.SEARCHES) == limit


# --- Notification hardening ------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "Eva\r\nBcc: victima@example.com",
    "Eva\nSubject: otra cosa",
    "Eva\x00nulo",
])
def test_crlf_in_a_name_cannot_reach_a_mail_header(client, hostile):
    from src.security.notify import header_safe

    cleaned = header_safe(hostile)
    assert "\r" not in cleaned and "\n" not in cleaned and "\x00" not in cleaned

    # And the request itself must still succeed rather than 500.
    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    response = client.post("/auth/request", json={"password": "clave-de-prueba", "name": hostile})
    assert response.status_code == 200


# --- Forwarding headers ----------------------------------------------------

def test_forwarding_headers_are_ignored_unless_trusted(client):
    from src.security.config import settings

    assert settings.trust_proxy_headers is False, "el default debe ser no fiarse"

    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    # Rotating the header must not hand out a fresh rate-limit bucket.
    codes = [
        client.post(
            "/auth/request",
            json={"password": "mala", "name": "bot"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        ).status_code
        for i in range(settings.auth_requests_per_hour + 5)
    ]
    assert 429 in codes


# --- Request size ----------------------------------------------------------

def test_oversized_body_is_refused_before_it_is_buffered(client):
    from src.security.config import settings

    client.post("/admin/api/web", headers=ADMIN, json={"enabled": True})
    approve(client, "Eva")
    oversized = str(settings.max_upload_mb * 1024 * 1024 + 10 * 1024 * 1024)
    response = client.post(
        "/transcribe_only",
        content=b"x",
        headers={"Content-Length": oversized, "Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413


# --- Audio duration --------------------------------------------------------

def test_unreadable_duration_is_refused(monkeypatch):
    """An unmeasurable file must not reach the paid transcription API."""
    from src.data_ingestion_transcription import transcriber

    monkeypatch.setattr(transcriber, "probe_duration", lambda path: 0.0)
    engine = transcriber.RemoteTranscriber(api_key="fake", base_url="http://unused")
    with pytest.raises(transcriber.TranscriptionError, match="duración"):
        engine.transcribe("/tmp/whatever.mp3")


def test_media_without_a_known_duration_is_refused(monkeypatch):
    from src.data_ingestion_transcription.audio_extractor import AudioExtractor
    from src.data_ingestion_transcription.transcriber import TranscriptionError

    extractor = AudioExtractor.__new__(AudioExtractor)
    monkeypatch.setattr(extractor, "_probe", lambda url: {"title": "directo"}, raising=False)
    with pytest.raises(TranscriptionError, match="duración"):
        extractor.download_and_extract_audio("https://example.com/live")
