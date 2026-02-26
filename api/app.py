import os, uuid, shutil
from fastapi import FastAPI, UploadFile, File, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import FileResponse
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
API_KEY    = os.getenv("API_KEY", "")          # Pflicht – in Coolify als Env-Variable setzen
UPLOAD_DIR = "/shared/uploads"
OUTPUT_DIR = "/shared/outputs"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="ffmpeg-api", version="2.0")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str = Security(api_key_header)):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY not configured on server")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key


async def get_redis():
    return await create_pool(RedisSettings(host=REDIS_HOST))


@app.get("/health")
def health():
    # Health-Endpoint bleibt offen (für Coolify-Healthcheck)
    return {"status": "ok"}


@app.post("/mp4-to-mp3")
async def mp4_to_mp3(
    file: UploadFile = File(...),
    _key: str = Security(require_api_key)
):
    """Nimmt eine Datei an, legt sie in die Queue und gibt eine Job-ID zurück."""
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Missing file")

    job_id = str(uuid.uuid4())
    in_suffix = os.path.splitext(file.filename)[1] or ".mp4"
    in_path = os.path.join(UPLOAD_DIR, f"{job_id}{in_suffix}")

    with open(in_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    pool = await get_redis()
    await pool.enqueue_job("convert_to_mp3", job_id, in_path)
    await pool.aclose()

    return {"job_id": job_id, "status": "queued"}


@app.get("/status/{job_id}")
async def get_status(
    job_id: str,
    _key: str = Security(require_api_key)
):
    """Gibt den Status eines Jobs zurück."""
    redis = await aioredis.from_url(f"redis://{REDIS_HOST}")
    status = await redis.get(f"job:{job_id}:status")
    error  = await redis.get(f"job:{job_id}:error")
    await redis.aclose()

    if not status:
        raise HTTPException(status_code=404, detail="Job not found")

    result = {"job_id": job_id, "status": status.decode()}
    if error:
        result["error"] = error.decode()
    return result


@app.get("/download/{job_id}")
async def download(
    job_id: str,
    _key: str = Security(require_api_key)
):
    """Gibt die fertige MP3-Datei zurück."""
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")

    if not os.path.exists(out_path):
        redis = await aioredis.from_url(f"redis://{REDIS_HOST}")
        status = await redis.get(f"job:{job_id}:status")
        await redis.aclose()
        if not status:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(status_code=202, detail=f"Job status: {status.decode()}")

    return FileResponse(out_path, media_type="audio/mpeg", filename="audio.mp3")
