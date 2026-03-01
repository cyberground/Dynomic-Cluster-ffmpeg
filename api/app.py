import os
import uuid
import shutil

from fastapi import FastAPI, UploadFile, File, HTTPException, Security, Request, BackgroundTasks
from fastapi.security import APIKeyHeader
from fastapi.responses import FileResponse
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
API_KEY    = os.getenv("API_KEY", "")
UPLOAD_DIR = "/shared/uploads"
OUTPUT_DIR = "/shared/outputs"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="ffmpeg-api", version="2.1")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# App Lifecycle – Connections einmalig aufbauen, nicht pro Request
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    if not API_KEY:
        raise RuntimeError("API_KEY environment variable is not set")
    app.state.arq_pool = await create_pool(RedisSettings(host=REDIS_HOST))
    app.state.redis = await aioredis.from_url(f"redis://{REDIS_HOST}")


@app.on_event("shutdown")
async def shutdown():
    await app.state.arq_pool.aclose()
    await app.state.redis.aclose()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_api_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _remove_file(path: str):
    """Datei sicher löschen – für BackgroundTasks."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Health-Endpoint – offen für Coolify-Healthcheck."""
    return {"status": "ok"}


@app.post("/mp4-to-mp3")
async def mp4_to_mp3(
    request: Request,
    file: UploadFile = File(...),
    _key: str = Security(require_api_key)
):
    """
    Nimmt eine Datei an, legt sie in die Queue und gibt eine Job-ID zurück.
    Liest die Datei in 1MB-Chunks um den Event Loop nicht zu blockieren.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Missing file")

    job_id    = str(uuid.uuid4())
    in_suffix = os.path.splitext(file.filename)[1] or ".mp4"
    in_path   = os.path.join(UPLOAD_DIR, f"{job_id}{in_suffix}")

    # Async chunk-weises Schreiben
    with open(in_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1 MB Chunks
            f.write(chunk)

    await request.app.state.arq_pool.enqueue_job("convert_to_mp3", job_id, in_path)

    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
async def get_status(
    job_id: str,
    request: Request,
    _key: str = Security(require_api_key)
):
    """
    Gibt den Status eines Jobs zurück.
    Enthält auch Fortschritt (progress) und ggf. Fehlermeldung.
    """
    redis = request.app.state.redis

    status   = await redis.get(f"job:{job_id}:status")
    error    = await redis.get(f"job:{job_id}:error")
    progress = await redis.get(f"job:{job_id}:progress")

    if not status:
        raise HTTPException(status_code=404, detail="Job not found")

    result = {
        "job_id":   job_id,
        "status":   status.decode(),
        "progress": int(progress.decode()) if progress else 0,
    }

    if error:
        result["error"] = error.decode()

    return result


@app.get("/download/{job_id}")
async def download(
    job_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _key: str = Security(require_api_key)
):
    """
    Gibt die fertige MP3-Datei zurück.
    Löscht die Datei nach dem Download automatisch im Hintergrund.
    """
    redis    = request.app.state.redis
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")

    if not os.path.exists(out_path):
        status = await redis.get(f"job:{job_id}:status")
        if not status:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(
            status_code=202,
            detail=f"Job status: {status.decode()}"
        )

    # Datei nach Download im Hintergrund löschen
    background_tasks.add_task(_remove_file, out_path)

    return FileResponse(
        out_path,
        media_type="audio/mpeg",
        filename="audio.mp3"
    )
