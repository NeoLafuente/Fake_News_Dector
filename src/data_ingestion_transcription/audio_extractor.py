"""Media ingestion: pull audio from a URL, hand it to the transcription API.

Downloads are bounded before a single byte is fetched — duration is read from
the metadata probe and the download is refused if the media is too long.
"""
import os
import re
import tempfile
import uuid
from typing import Optional

import yt_dlp

from src.security.config import settings
from .transcriber import RemoteTranscriber, TranscriptionError

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/factx-downloads")
# The stem forbids dots outright so no combination of inputs can produce a
# ".." sequence; the extension keeps a single leading dot and nothing else.
_SAFE_STEM = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_EXT = re.compile(r"[^A-Za-z0-9]")


def safe_filename(name: str, fallback_ext: str = ".bin") -> str:
    """Build a filename that cannot escape the download directory.

    A client-supplied name like '../../etc/passwd' must not decide where we
    write, so only the basename survives and it gets a random prefix.
    """
    # Windows-style separators are not separators on Linux, so strip both.
    base = os.path.basename((name or "").replace("\\", "/").rsplit("/", 1)[-1])
    stem_raw, ext_raw = os.path.splitext(base)

    ext = _SAFE_EXT.sub("", ext_raw)[:9]
    ext = f".{ext}" if ext else fallback_ext
    stem = _SAFE_STEM.sub("_", stem_raw)[:60].strip("_") or "upload"
    return f"{uuid.uuid4().hex[:12]}_{stem}{ext}"


class AudioExtractor:
    """Facade kept deliberately compatible with the previous local-Whisper one."""

    def __init__(self, transcriber: Optional[RemoteTranscriber] = None):
        self.transcriber = transcriber or RemoteTranscriber()
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    def _probe(self, url: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    def download_and_extract_audio(self, url: str, output_path: str = DOWNLOAD_DIR) -> str:
        os.makedirs(output_path, exist_ok=True)

        info = self._probe(url)
        duration = info.get("duration")
        # Fail closed. Treating an unknown duration as acceptable would let a
        # live stream or a site that hides its metadata walk straight past the
        # limit and start an unbounded download.
        if not duration:
            raise TranscriptionError(
                "No se puede determinar la duración de ese medio (¿es una retransmisión "
                "en directo?), así que se rechaza por precaución. Sube el archivo o usa "
                "un enlace a un vídeo con duración conocida."
            )
        if duration > settings.max_audio_seconds:
            raise TranscriptionError(
                f"El vídeo dura {int(duration)}s y el máximo permitido son "
                f"{settings.max_audio_seconds}s."
            )

        stem = os.path.join(output_path, uuid.uuid4().hex)
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"{stem}.%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": settings.max_upload_mb * 1024 * 1024 * 4,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        produced = f"{stem}.mp3"
        if os.path.exists(produced):
            return produced
        # Fall back to whatever extension the postprocessor actually emitted.
        directory = os.path.dirname(stem)
        prefix = os.path.basename(stem)
        for entry in os.listdir(directory):
            if entry.startswith(prefix):
                return os.path.join(directory, entry)
        raise TranscriptionError("No se pudo descargar el audio de esa URL.")

    def transcribe(self, audio_path: str) -> str:
        return self.transcriber.transcribe(audio_path)

    def process_url(self, url: str) -> str:
        audio_file = self.download_and_extract_audio(url)
        try:
            return self.transcribe(audio_file)
        finally:
            if os.path.exists(audio_file):
                os.remove(audio_file)

    def save_upload(self, upload) -> str:
        """Persist an UploadFile under a sanitised name, enforcing the size cap."""
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        target = os.path.join(DOWNLOAD_DIR, safe_filename(upload.filename, ".mp3"))
        limit = settings.max_upload_mb * 1024 * 1024
        written = 0
        with open(target, "wb") as handle:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    handle.close()
                    os.remove(target)
                    raise TranscriptionError(
                        f"El archivo supera el máximo de {settings.max_upload_mb} MB."
                    )
                handle.write(chunk)
        return target
