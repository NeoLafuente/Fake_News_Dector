"""Central configuration for the access-control and cost-control layer.

Every knob lives in the environment so the owner can change behaviour without
rebuilding the image. See .env.example for the full list.
"""
import os
from dataclasses import dataclass, field


def _b(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # --- Identity / secrets -------------------------------------------------
    secret_key: str = field(default_factory=lambda: _s("SECRET_KEY"))
    gate_password: str = field(default_factory=lambda: _s("GATE_PASSWORD"))
    admin_token: str = field(default_factory=lambda: _s("ADMIN_TOKEN"))
    owner_email: str = field(default_factory=lambda: _s("OWNER_EMAIL", "neolafuente@gmail.com"))

    # --- Session policy -----------------------------------------------------
    session_minutes: int = field(default_factory=lambda: _i("SESSION_MINUTES", 10))
    request_ttl_minutes: int = field(default_factory=lambda: _i("REQUEST_TTL_MINUTES", 15))
    web_enabled_on_boot: bool = field(default_factory=lambda: _b("WEB_ENABLED_ON_BOOT", "false"))
    max_pending_requests: int = field(default_factory=lambda: _i("MAX_PENDING_REQUESTS", 20))
    auth_requests_per_hour: int = field(default_factory=lambda: _i("AUTH_REQUESTS_PER_HOUR", 5))
    public_base_url: str = field(default_factory=lambda: _s("PUBLIC_BASE_URL", "http://localhost:8000"))
    cookie_secure: bool = field(default_factory=lambda: _b("COOKIE_SECURE", "true"))
    # Forwarding headers are forgeable by anyone who can reach the origin
    # directly, so they are ignored unless the deployment really is behind a
    # proxy that overwrites them (the Cloudflare tunnel does).
    trust_proxy_headers: bool = field(default_factory=lambda: _b("TRUST_PROXY_HEADERS", "false"))
    db_path: str = field(default_factory=lambda: _s("SECURITY_DB_PATH", "/data/security.db"))

    # --- Notification channels ---------------------------------------------
    smtp_host: str = field(default_factory=lambda: _s("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: _i("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _s("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _s("SMTP_PASSWORD"))
    telegram_bot_token: str = field(default_factory=lambda: _s("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _s("TELEGRAM_CHAT_ID"))

    # --- Cost ceilings ------------------------------------------------------
    max_transcript_chars: int = field(default_factory=lambda: _i("MAX_TRANSCRIPT_CHARS", 6000))
    max_facts_per_run: int = field(default_factory=lambda: _i("MAX_FACTS_PER_RUN", 8))
    max_searches_per_run: int = field(default_factory=lambda: _i("MAX_SEARCHES_PER_RUN", 8))
    max_runs_per_session: int = field(default_factory=lambda: _i("MAX_RUNS_PER_SESSION", 3))
    daily_llm_call_budget: int = field(default_factory=lambda: _i("DAILY_LLM_CALL_BUDGET", 80))
    daily_search_budget: int = field(default_factory=lambda: _i("DAILY_SEARCH_BUDGET", 150))
    daily_transcription_budget: int = field(default_factory=lambda: _i("DAILY_TRANSCRIPTION_BUDGET", 60))
    max_audio_seconds: int = field(default_factory=lambda: _i("MAX_AUDIO_SECONDS", 180))
    max_upload_mb: int = field(default_factory=lambda: _i("MAX_UPLOAD_MB", 20))
    search_provider: str = field(default_factory=lambda: _s("SEARCH_PROVIDER", "duckduckgo"))

    def require(self) -> None:
        """Fail fast at boot rather than silently running without protection."""
        missing = [n for n in ("secret_key", "gate_password", "admin_token") if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                "Faltan variables obligatorias en el entorno: "
                + ", ".join(m.upper() for m in missing)
                + ". Genera valores con: openssl rand -hex 32"
            )
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY debe tener al menos 32 caracteres.")


settings = Settings()
