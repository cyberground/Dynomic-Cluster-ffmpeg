import os, subprocess
import redis.asyncio as aioredis
from arq.connections import RedisSettings

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
OUTPUT_DIR = "/shared/outputs"

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
            # Input-Datei aufräumen
            try:
                os.remove(in_path)
            except Exception:
                pass

    except Exception as e:
        await redis.set(f"job:{job_id}:status", "failed")
        await redis.set(f"job:{job_id}:error", str(e))
    finally:
        await redis.aclose()


# arq Worker-Konfiguration
class WorkerSettings:
    functions = [convert_to_mp3]
    redis_settings = RedisSettings(host=REDIS_HOST)
    max_jobs = int(os.getenv("MAX_JOBS", "2"))     # Gleichzeitige Jobs pro Worker
    queue_name = "arq:queue"
    job_timeout = 600                               # Max. 10 Min. pro Job
    keep_result = 3600                              # Ergebnis 1h in Redis halten
