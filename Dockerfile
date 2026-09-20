# syntax=docker/dockerfile:1.7

###############################################################################
# Stage 1 — export the NLI cross-encoder to int8 ONNX.
#
# torch and optimum are needed to perform the export and nowhere else, so they
# stay in this stage and never reach the published image.
###############################################################################
FROM python:3.13-slim AS nli-builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.*" \
 && pip install "optimum[onnxruntime]>=1.23" "transformers>=4.45,<6.0" sentencepiece

COPY scripts/export_nli_onnx.py /build/export_nli_onnx.py
RUN python /build/export_nli_onnx.py --output /models/nli-onnx


###############################################################################
# Stage 2 — install runtime dependencies into a self-contained virtualenv.
###############################################################################
FROM python:3.13-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-serve.txt /tmp/requirements-serve.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements-serve.txt


###############################################################################
# Stage 3 — the image that actually ships.
###############################################################################
FROM python:3.13-slim AS runtime

# ffmpeg/ffprobe are required by yt-dlp and by the audio downsampling step.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    NLI_MODEL_DIR=/models/nli-onnx \
    SECURITY_DB_PATH=/data/security.db \
    DOWNLOAD_DIR=/tmp/factx-downloads \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY --from=deps      /opt/venv       /opt/venv
COPY --from=nli-builder /models/nli-onnx /models/nli-onnx

WORKDIR /app
COPY src/ /app/src/

# Unprivileged runtime user; /data is the only writable mount it needs.
RUN useradd --create-home --uid 10001 factx \
 && mkdir -p /data /tmp/factx-downloads \
 && chown -R factx:factx /data /tmp/factx-downloads /app
USER factx

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Single worker on purpose: the SQLite state store and the kill switch assume
# one process, and this workload is I/O bound on external APIs anyway.
# Uvicorn's proxy-header handling rewrites request.client.host from
# X-Forwarded-For before any application code runs, so enabling it
# unconditionally would make the application-level TRUST_PROXY_HEADERS switch
# cosmetic: a directly reachable origin could still be spoofed. Both layers are
# therefore driven by the same variable, and both default to off.
CMD ["sh", "-c", "\
if [ \"$TRUST_PROXY_HEADERS\" = \"true\" ]; then \
  set -- --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\"; \
else \
  set -- ; \
fi; \
exec uvicorn src.app:app --host 0.0.0.0 --port ${PORT} --workers 1 \"$@\"" ]
