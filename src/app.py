import os
import sys
from typing import Optional

# Add the project root to sys.path so 'src' can be imported easily from anywhere
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.security import budget, middleware, routes_admin, routes_auth, store
from src.security.config import settings

# Refuse to boot unprotected: no secret key, no password, no admin token, no app.
settings.require()
store.init()

app = FastAPI(
    title="Fact Detection System API",
    description="Agentic fact-checking behind owner-approved, time-limited sessions.",
)

# Same-origin only. The previous wildcard let any site drive the API with a
# visitor's cookie.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_base_url],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)

middleware.install(app)
app.include_router(routes_auth.router)
app.include_router(routes_admin.router)

static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_path, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_path), name="static")


class TextRequest(BaseModel):
    transcript: str = Field(min_length=1)


# Both are expensive to construct, so they are built on first use rather than
# at import time — it keeps cold starts short and lets the app boot without keys.
_audio_extractor = None
_graph_app = None


def get_audio_extractor():
    global _audio_extractor
    if _audio_extractor is None:
        from src.data_ingestion_transcription.audio_extractor import AudioExtractor

        _audio_extractor = AudioExtractor()
    return _audio_extractor


def get_graph():
    global _graph_app
    if _graph_app is None:
        from src.agent_orchestrator.graph import build_graph

        _graph_app = build_graph()
    return _graph_app


def consume_run(request: Request) -> str:
    """Charge one analysis against the caller's session quota.

    The reservation is conditional, so a refused request does not consume a
    slot and two concurrent requests cannot share the last one.
    """
    sid = middleware.current_session_id(request)
    if sid is None:
        raise HTTPException(status_code=401, detail="Sesión no válida.")
    if not store.try_consume_run(sid, settings.max_runs_per_session):
        raise HTTPException(
            status_code=429,
            detail=f"Has agotado los {settings.max_runs_per_session} análisis de esta sesión.",
        )
    return sid


def run_pipeline(transcript: str) -> dict:
    initial_state = {
        "raw_transcript": transcript,
        "extracted_facts": [],
        "search_results": {},
        "final_nli_analysis": [],
    }
    result_state = get_graph().invoke(initial_state)
    return {"transcript": transcript, "results": result_state["final_nli_analysis"]}


@app.exception_handler(budget.BudgetExceeded)
async def budget_handler(request: Request, exc: budget.BudgetExceeded):
    return JSONResponse({"detail": str(exc), "code": "budget_exceeded"}, status_code=429)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "web_enabled": store.is_web_enabled()}


@app.post("/transcribe_only")
def transcribe_only_endpoint(  # sync on purpose: runs in the threadpool
    request: Request,
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    """Transcribe a media URL or an uploaded file, skipping the agent pipeline."""
    consume_run(request)
    extractor = get_audio_extractor()
    try:
        if file:
            path = extractor.save_upload(file)
            try:
                return {"transcript": extractor.transcribe(path)}
            finally:
                os.path.exists(path) and os.remove(path)
        if url:
            return {"transcript": extractor.process_url(url)}
        raise HTTPException(status_code=400, detail="Debes proporcionar una URL o subir un archivo.")
    except budget.BudgetExceeded:
        raise
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/process_url")
def process_url_endpoint(  # sync on purpose: runs in the threadpool
    request: Request,
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    """Full pipeline: ingest, transcribe, extract claims, search, evaluate."""
    consume_run(request)
    extractor = get_audio_extractor()
    try:
        if file:
            path = extractor.save_upload(file)
            try:
                transcript = extractor.transcribe(path)
            finally:
                os.path.exists(path) and os.remove(path)
        elif url:
            transcript = extractor.process_url(url)
        else:
            raise HTTPException(status_code=400, detail="Debes proporcionar una URL o subir un archivo.")

        return run_pipeline(transcript)
    except budget.BudgetExceeded:
        raise
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/analyze_text")
def analyze_text_endpoint(payload: TextRequest, request: Request):  # threadpool
    """Run the agent pipeline directly on text, with no audio stage."""
    consume_run(request)
    try:
        return run_pipeline(payload.transcript)
    except budget.BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon")


@app.get("/", include_in_schema=False)
async def serve_frontend(request: Request):
    """The app itself for an approved session, the gate for everyone else."""
    if store.is_web_enabled() and middleware.current_session_id(request) is not None:
        return FileResponse(os.path.join(static_path, "index.html"))
    return FileResponse(os.path.join(static_path, "login.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("DEV_RELOAD", "false").lower() == "true",
    )
