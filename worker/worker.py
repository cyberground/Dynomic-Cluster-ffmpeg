import asyncio
import os
import time

from arq.connections import RedisSettings
from arq.cron import cron

REDIS_HOST  = os.getenv("REDIS_HOST", "redis")
OUTPUT_DIR  = "/shared/outputs"
UPLOAD_DIR  = "/shared/uploads"
FILE_TTL    = int(os.getenv("FILE_TTL_SECONDS", "3600"))   # Standard: 1 Stunde
JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", "1800"))        # Standard: 30 Minuten

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)


async def convert_to_mp3(ctx, job_id: str, in_path: str):
    """
    Wird vom arq-Worker aufgerufen.
    Konvertiert die Eingabedatei nach MP3 und speichert Status in Redis.
    Nutzt asyncio.create_subprocess_exec um den Event Loop nicht zu blockieren.
    """
    redis = ctx["redis"]
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")

    try:
        await redis.set(f"job:{job_id}:status", "processing")
        await redis.set(f"job:{job_id}:progress", "0")

        cmd = [
            "ffmpeg", "-y",
            "-i", in_path,
            "-vn",              # Kein Video
            "-ac", "1",         # Mono
            "-ar", "16000",     # 16kHz Sample Rate
            "-b:a", "64k",      # 64k Bitrate
            out_path
        ]

        # Nicht-blockierender Subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", "ignore")[:1200]
            await redis.set(f"job:{job_id}:status", "failed")
            await redis.set(f"job:{job_id}:error", error_msg)
        else:
            await redis.set(f"job:{job_id}:status", "done")
            await redis.set(f"job:{job_id}:progress", "100")
            await redis.set(f"job:{job_id}:expires_at", str(time.time() + FILE_TTL))

    except asyncio.CancelledError:
        # Job wurde abgebrochen (z.B. durch Timeout)
        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", "Job cancelled or timed out")
        raise

    except Exception as e:
        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", str(e))

    finally:
        # Input-Datei immer aufräumen
        try:
            os.remove(in_path)
        except FileNotFoundError:
            pass


async def cleanup_old_files(ctx):
    """
    Scheduled Task – läuft alle 15 Minuten.
    Löscht MP3-Dateien und Redis-Keys deren TTL abgelaufen ist.
    Nutzt SCAN statt KEYS um Redis nicht zu blockieren.
    """
    redis = ctx["redis"]
    now = time.time()
    deleted = 0

    # SCAN statt KEYS – blockiert Redis nicht bei vielen Keys
    async for key in redis.scan_iter("job:*:expires_at"):
        expires_at = await redis.get(key)
        if not expires_at:
            continue

        if float(expires_at) < now:
            job_id = key.decode().split(":")[1]
            out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp3")

            # Datei löschen
            try:
                os.remove(out_path)
                deleted += 1
            except FileNotFoundError:
                pass

            # Alle Redis-Keys dieses Jobs löschen
            await redis.delete(
                f"job:{job_id}:status",
                f"job:{job_id}:error",
                f"job:{job_id}:progress",
                f"job:{job_id}:expires_at"
            )

    return {"deleted": deleted}


# arq Worker-Konfiguration
class WorkerSettings:
    functions      = [convert_to_mp3]
    cron_jobs      = [
        cron(cleanup_old_files, minute={0, 15, 30, 45})
    ]
    redis_settings = RedisSettings(host=REDIS_HOST)
    max_jobs       = int(os.getenv("MAX_JOBS", "1"))
    queue_name     = "arq:queue"
    job_timeout    = JOB_TIMEOUT
    keep_result    = 3600
