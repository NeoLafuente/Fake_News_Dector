"""Remote speech-to-text over an OpenAI-compatible /audio/transcriptions API.

Dropping local Whisper is what removes `torch` from the runtime image (~2.5 GB)
and what makes cold starts survivable. Groq serves whisper-large-v3-turbo on a
free tier with no card; any other OpenAI-compatible endpoint works by pointing
TRANSCRIPTION_BASE_URL somewhere else.
"""
import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional

import requests

from src.security import budget
from src.security.config import settings

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "whisper-large-v3-turbo"

# Providers cap the upload; we re-encode well below it and still guard.
UPLOAD_LIMIT_BYTES = 24 * 1024 * 1024


class TranscriptionError(RuntimeError):
    pass


def _ffmpeg(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise TranscriptionError(
            f"'{name}' no está instalado. Es necesario para procesar audio."
        )
    return path


def probe_duration(audio_path: str) -> float:
    """Length in seconds, or 0.0 when ffprobe cannot tell."""
    try:
        out = subprocess.run(
            [
                _ffmpeg("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                "-of", "json", audio_path,
            ],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001 - a missing duration must not be fatal
        return 0.0


def compress_for_upload(audio_path: str) -> str:
    """Re-encode to 16 kHz mono MP3 @32 kbps — what speech models want anyway.

    An hour of audio lands around 14 MB, so the provider's size cap stops being
    a practical limit and the upload stays fast.
    """
    handle, target = tempfile.mkstemp(suffix=".mp3", dir=os.path.dirname(audio_path) or None)
    os.close(handle)
    try:
        subprocess.run(
            [
                _ffmpeg("ffmpeg"), "-y", "-i", audio_path,
                "-ac", "1", "-ar", "16000", "-b:a", "32k", "-vn", target,
            ],
            capture_output=True, timeout=600, check=True,
        )
    except subprocess.CalledProcessError as exc:
        os.path.exists(target) and os.remove(target)
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-400:]
        raise TranscriptionError(f"ffmpeg no pudo convertir el audio: {stderr}") from exc
    return target


class RemoteTranscriber:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("TRANSCRIPTION_API_KEY", "").strip()
        self.base_url = (base_url or os.environ.get("TRANSCRIPTION_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.environ.get("TRANSCRIPTION_MODEL", DEFAULT_MODEL)

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> str:
        if not self.api_key:
            raise TranscriptionError(
                "Falta TRANSCRIPTION_API_KEY. Crea una gratis en console.groq.com."
            )

        duration = probe_duration(audio_path)
        if duration and duration > settings.max_audio_seconds:
            raise TranscriptionError(
                f"El audio dura {int(duration)}s y el máximo permitido son "
                f"{settings.max_audio_seconds}s. Recorta el fragmento."
            )

        # Claim the daily slot before spending anything upstream.
        budget.claim(budget.TRANSCRIPTIONS)

        compressed = compress_for_upload(audio_path)
        try:
            size = os.path.getsize(compressed)
            if size > UPLOAD_LIMIT_BYTES:
                raise TranscriptionError(
                    f"El audio comprimido sigue pesando {size // 1024 // 1024} MB. Usa un fragmento más corto."
                )

            data = {"model": self.model, "response_format": "json"}
            if language:
                data["language"] = language

            with open(compressed, "rb") as handle:
                response = requests.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (os.path.basename(compressed), handle, "audio/mpeg")},
                    data=data,
                    timeout=300,
                )

            if response.status_code == 429:
                raise TranscriptionError(
                    "El proveedor de transcripción ha limitado la cuota gratuita. Inténtalo más tarde."
                )
            if response.status_code >= 400:
                raise TranscriptionError(
                    f"Error {response.status_code} del servicio de transcripción: {response.text[:300]}"
                )

            budget.record(budget.AUDIO_SECONDS, int(duration))
            return (response.json().get("text") or "").strip()
        finally:
            if os.path.exists(compressed):
                os.remove(compressed)
