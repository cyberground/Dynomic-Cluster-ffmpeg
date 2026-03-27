import os
import uuid
import shutil

from fastapi import FastAPI, UploadFile, File, HTTPException, Security, Request, BackgroundTasks
from fastapi.security import APIKeyHeader
from fastapi.responses import FileResponse
from pydantic import BaseModel
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
API_KEY    = os.getenv("API_KEY", "")
UPLOAD_DIR = "/shared/uploads"
OUTPUT_DIR = "/shared/outputs"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="ffmpeg-api", version="2.2")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# App Lifecycle
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
# Models
# ---------------------------------------------------------------------------

class PathRequest(BaseModel):
    file_path: str

class YouTubeRequest(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _remove_file(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    import subprocess
    checks = {"status": "ok"}

    # yt-dlp check
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        checks["yt_dlp"] = r.stdout.strip() if r.returncode == 0 else "ERROR: " + r.stderr[:100]
    except Exception as e:
        checks["yt_dlp"] = f"NOT INSTALLED: {e}"

    # Node.js check (JS Runtime fuer yt-dlp)
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        checks["nodejs"] = r.stdout.strip() if r.returncode == 0 else "ERROR"
    except Exception as e:
        checks["nodejs"] = f"NOT INSTALLED: {e}"

    # ffmpeg check
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        checks["ffmpeg"] = r.stdout.split('\n')[0] if r.returncode == 0 else "ERROR"
    except Exception as e:
        checks["ffmpeg"] = f"NOT INSTALLED: {e}"

    return checks


@app.post("/mp4-to-mp3")
async def mp4_to_mp3(
    request: Request,
    file: UploadFile = File(...),
    _key: str = Security(require_api_key)
):
    """
    Klassischer Upload via multipart-form-data.
    Liest die Datei in 1MB-Chunks um den Event Loop nicht zu blockieren.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Missing file")

    job_id    = str(uuid.uuid4())
    in_suffix = os.path.splitext(file.filename)[1] or ".mp4"
    in_path   = os.path.join(UPLOAD_DIR, f"{job_id}{in_suffix}")

    with open(in_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    await request.app.state.arq_pool.enqueue_job("convert_to_mp3", job_id, in_path)

    return {"job_id": job_id, "status": "queued"}


@app.post("/path-to-mp3")
async def path_to_mp3(
    request: Request,
    body: PathRequest,
    _key: str = Security(require_api_key)
):
    """
    Neuer Endpoint: Datei liegt bereits auf dem shared Volume.
    n8n übergibt nur den Dateipfad – kein Upload, kein RAM-Problem.
    """
    if not os.path.exists(body.file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {body.file_path}")

    # Sicherheitscheck: Datei muss im erlaubten Verzeichnis liegen
    if not body.file_path.startswith("/shared/"):
        raise HTTPException(status_code=400, detail="file_path must be within /shared/")

    job_id = str(uuid.uuid4())
    await request.app.state.arq_pool.enqueue_job("convert_to_mp3", job_id, body.file_path)

    return {"job_id": job_id, "status": "queued"}


@app.post("/youtube-to-mp3")
async def youtube_to_mp3(
    request: Request,
    body: YouTubeRequest,
    _key: str = Security(require_api_key)
):
    """
    YouTube-URL entgegennehmen, Audio via yt-dlp extrahieren,
    dann an den Worker zur MP3-Konvertierung weiterleiten.
    """
    import subprocess
    import re

    url = body.url.strip()

    # Validierung: nur YouTube-URLs
    if not re.match(r'https?://(www\.)?(youtube\.com|youtu\.be)/', url):
        raise HTTPException(status_code=400, detail="Nur YouTube-URLs erlaubt")

    job_id = str(uuid.uuid4())
    dl_path = os.path.join(UPLOAD_DIR, f"{job_id}.%(ext)s")

    # Status setzen
    redis = request.app.state.redis
    await redis.set(f"job:{job_id}:status", "downloading")

    try:
        # yt-dlp: Nur Audio extrahieren, bestes Format
        result = subprocess.run(
            [
                "yt-dlp",
                "--js-runtimes", "node",
                "--no-playlist",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "5",
                "-o", os.path.join(UPLOAD_DIR, f"{job_id}.%(ext)s"),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            await redis.set(f"job:{job_id}:status", "failed")
            await redis.set(f"job:{job_id}:error", result.stderr[:500])
            return {"job_id": job_id, "status": "failed", "error": result.stderr[:200]}

        # yt-dlp schreibt die Datei mit --extract-audio --audio-format mp3
        # Dateiname finden
        mp3_path = os.path.join(UPLOAD_DIR, f"{job_id}.mp3")
        if not os.path.exists(mp3_path):
            # Suche nach der tatsaechlichen Datei
            for f in os.listdir(UPLOAD_DIR):
                if f.startswith(job_id):
                    actual_path = os.path.join(UPLOAD_DIR, f)
                    # Falls nicht MP3, durch Worker konvertieren lassen
                    if not f.endswith(".mp3"):
                        await request.app.state.arq_pool.enqueue_job("convert_to_mp3", job_id, actual_path)
                        return {"job_id": job_id, "status": "queued"}
                    mp3_path = actual_path
                    break

        if os.path.exists(mp3_path):
            # MP3 direkt in outputs verschieben
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")
            shutil.move(mp3_path, out_path)
            await redis.set(f"job:{job_id}:status", "done")
            await redis.set(f"job:{job_id}:progress", "100")
            import time
            await redis.set(f"job:{job_id}:expires_at", str(time.time() + 3600))
            return {"job_id": job_id, "status": "done"}

        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", "Download completed but output file not found")
        return {"job_id": job_id, "status": "failed"}

    except subprocess.TimeoutExpired:
        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", "YouTube download timeout (5min)")
        return {"job_id": job_id, "status": "failed"}
    except Exception as e:
        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", str(e)[:500])
        return {"job_id": job_id, "status": "failed"}


@app.get("/status/{job_id}")
async def get_status(
    job_id: str,
    request: Request,
    _key: str = Security(require_api_key)
):
    redis    = request.app.state.redis
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

    background_tasks.add_task(_remove_file, out_path)

    return FileResponse(
        out_path,
        media_type="audio/mpeg",
        filename="audio.mp3"
    )
