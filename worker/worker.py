import os, subprocess, time
import redis.asyncio as aioredis
from arq.connections import RedisSettings

REDIS_HOST  = os.getenv("REDIS_HOST", "redis")
OUTPUT_DIR  = "/shared/outputs"
UPLOAD_DIR  = "/shared/uploads"
FILE_TTL    = int(os.getenv("FILE_TTL_SECONDS", "3600"))   # Standard: 1 Stunde

os.makedirs(OUTPUT_DIR, exist_ok=True)


async def convert_to_mp3(ctx, job_id: str, in_path: str):
    """
    Wird vom arq-Worker aufgerufen.
    Konvertiert die Eingabedatei nach MP3 und speichert Status in Redis.
    """
    redis = await aioredis.from_url(f"redis://{REDIS_HOST}")
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")

    try:
        await redis.set(f"job:{job_id}:status", "processing")

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            out_path
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if p.returncode != 0:
            error_msg = p.stderr.decode("utf-8", "ignore")[:1200]
            await redis.set(f"job:{job_id}:status", "failed")
            await redis.set(f"job:{job_id}:error", error_msg)
        else:
            await redis.set(f"job:{job_id}:status", "done")
            # Ablaufzeit für automatisches Cleanup speichern
            await redis.set(f"job:{job_id}:expires_at", str(time.time() + FILE_TTL))

        # Input-Datei sofort aufräumen
        try:
            os.remove(in_path)
        except Exception:
            pass

    except Exception as e:
        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", str(e))
    finally:
        await redis.aclose()


async def cleanup_old_files(ctx):
    """
    Scheduled Task – läuft alle 15 Minuten.
    Löscht MP3-Dateien und Redis-Keys deren TTL abgelaufen ist.
    """
    redis = await aioredis.from_url(f"redis://{REDIS_HOST}")
    now = time.time()
    deleted = 0

    # Alle Expire-Keys aus Redis holen
    keys = await redis.keys("job:*:expires_at")
    for key in keys:
        expires_at = await redis.get(key)
        if expires_at and float(expires_at) < now:
            job_id = key.decode().split(":")[1]
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")

            # Datei löschen
            try:
                os.remove(out_path)
                deleted += 1
            except FileNotFoundError:
                pass

            # Redis-Keys löschen
            await redis.delete(
                f"job:{job_id}:status",
                f"job:{job_id}:error",
                f"job:{job_id}:expires_at"
            )

    await redis.aclose()
    return {"deleted": deleted}


# arq Worker-Konfiguration
class WorkerSettings:
    functions       = [convert_to_mp3]
    cron_jobs       = [
        # Cleanup alle 15 Minuten
        {"coroutine": cleanup_old_files, "minute": {0, 15, 30, 45}}
    ]
    redis_settings  = RedisSettings(host=REDIS_HOST)
    max_jobs        = int(os.getenv("MAX_JOBS", "2"))
    queue_name      = "arq:queue"
    job_timeout     = 600
    keep_result     = 3600
