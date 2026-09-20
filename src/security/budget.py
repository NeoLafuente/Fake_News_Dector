"""Hard ceilings on everything that can generate a bill.

The rule is simple: no external call happens unless a budget slot was claimed
first. Counters are per-UTC-day and live in SQLite, so restarting the container
does not hand anyone a fresh allowance.
"""
from typing import Dict

from . import store
from .config import settings

LLM_CALLS = "llm_calls"
SEARCHES = "searches"
TRANSCRIPTIONS = "transcriptions"
AUDIO_SECONDS = "audio_seconds"


class BudgetExceeded(RuntimeError):
    """Raised when a daily ceiling would be crossed."""


_LIMITS = {
    LLM_CALLS: lambda: settings.daily_llm_call_budget,
    SEARCHES: lambda: settings.daily_search_budget,
    TRANSCRIPTIONS: lambda: settings.daily_transcription_budget,
}


def remaining(key: str) -> int:
    limit = _LIMITS[key]()
    return max(0, limit - store.counter_value(key))


def claim(key: str, amount: int = 1) -> None:
    """Reserve `amount` units of a daily budget or raise BudgetExceeded."""
    limit = _LIMITS[key]()
    used = store.counter_value(key)
    if used + amount > limit:
        raise BudgetExceeded(
            f"Límite diario alcanzado para '{key}' ({used}/{limit}). "
            "Vuelve a intentarlo mañana o sube el límite en el .env."
        )
    store.counter_add(key, amount)


def record(key: str, amount: int = 1) -> None:
    """Count usage that has no ceiling of its own (e.g. audio seconds)."""
    store.counter_add(key, amount)


def snapshot() -> Dict[str, Dict[str, int]]:
    counters = store.all_counters()
    out: Dict[str, Dict[str, int]] = {}
    for key, limit_fn in _LIMITS.items():
        limit = limit_fn()
        used = counters.get(key, 0)
        out[key] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}
    out[AUDIO_SECONDS] = {
        "used": counters.get(AUDIO_SECONDS, 0),
        "limit": 0,
        "remaining": 0,
    }
    return out


def clamp_transcript(text: str) -> str:
    """Cap what reaches the LLM, which is what caps the token bill."""
    limit = settings.max_transcript_chars
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " […truncado por límite de coste]"
