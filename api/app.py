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

app = FastAPI(title="ffmpeg-api", version="3.2")

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
    cookies: str | None = None  # Netscape cookie format for bot-protection bypass
    # Welchen YouTube-Client yt-dlp vorgibt. Default bleibt "web" (Stand v3.1), aber
    # konfigurierbar: welcher Client durch die Bot-Pruefung kommt, aendert YouTube
    # laufend. Ohne diesen Schalter kostet jeder Versuch einen Redeploy.
    # "" (leer) laesst yt-dlp seine eigenen Defaults waehlen.
    player_client: str | None = None
    # Format-Auswahl fuer yt-dlp (-f). Default "bestaudio/best": dieser Endpunkt will
    # ausschliesslich Audio, die yt-dlp-Voreinstellung verlangt dagegen Video UND Audio
    # und scheitert mit "Requested format is not available", sobald YouTube fuer den
    # gewaehlten Client keine passende Kombination anbietet. Am 2026-08-17 mit frischen
    # Cookies reproduziert: die Bot-Pruefung war ueberwunden, die Formatwahl scheiterte
    # bei JEDEM Client identisch. Konfigurierbar, damit Nachjustieren keinen Redeploy kostet.
    format_selector: str | None = None

class TranscriptRequest(BaseModel):
    url: str
    language: str = "de"  # preferred language (de, en, auto)
    # Netscape-Cookie-Format, wie bei /youtube-to-mp3. YouTube beantwortet Anfragen aus
    # Rechenzentrums-Netzen sonst mit einer Bot-Pruefung ("Sign in to confirm you're not
    # a bot") — das trifft den Untertitel-Abruf genauso wie den Download, obwohl dieser
    # Endpunkt bisher gar keine Cookies annehmen konnte.
    cookies: str | None = None


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


def _session_with_cookies(cookies: str | None):
    """requests.Session mit Netscape-Cookies, oder None wenn keine uebergeben wurden.

    Gibt bewusst None zurueck statt einer leeren Session: youtube-transcript-api legt
    sich dann selbst eine an. Schlaegt das Einlesen fehl, wird das GEMELDET und nicht
    still ignoriert — sonst sucht man den Bot-Check spaeter an der falschen Stelle.
    """
    if not cookies or not cookies.strip():
        return None
    import http.cookiejar, tempfile, os as _os
    import requests
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix="_cookies.txt")
        with _os.fdopen(fd, "w") as fh:
            fh.write(cookies)
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=True)
        sess = requests.Session()
        sess.cookies = jar
        return sess
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Cookies nicht lesbar (Netscape-Format erwartet): {type(e).__name__}: {e}",
        )
    finally:
        if path:
            _remove_file(path)


def _yt_error_detail(stderr: str, limit: int = 1500) -> str:
    """Die eigentliche Ursache aus yt-dlp-stderr herausziehen.

    yt-dlp schreibt Warnungen (Version, Player-Clients) an den ANFANG und den
    tatsaechlichen Fehler ans ENDE. Ein simples stderr[:200] liefert deshalb
    zuverlaessig nur die Warnung — und eine Fehlermeldung, die die Ursache
    nicht enthaelt, ist schlimmer als keine, weil sie die Suche in die falsche
    Richtung schickt.
    """
    lines = [l.rstrip() for l in (stderr or "").splitlines() if l.strip()]
    if not lines:
        return "yt-dlp ist ohne Ausgabe fehlgeschlagen"
    errors = [l for l in lines if l.lstrip().upper().startswith("ERROR")]
    chosen = errors if errors else lines[-8:]
    return "\n".join(chosen)[:limit]


def _ytdlp_transcript(url: str, language: str, cookies: str | None) -> dict | None:
    """Untertitel ueber yt-dlp holen, statt ueber youtube-transcript-api.

    WARUM ES DIESEN ZWEITEN WEG GIBT (2026-08-17):
    youtube-transcript-api spricht eine YouTube-Schnittstelle an, die Anfragen aus
    Rechenzentrums-Netzen hart blockt — auch MIT gueltigen Cookies. Gemessen: derselbe
    Cluster, dieselben frischen Cookies, /youtube-to-mp3 kommt durch, der Untertitel-Abruf
    nicht. yt-dlp nutzt einen anderen Pfad und ist bereits authentifiziert.

    Kein Download der Mediendatei (--skip-download): geholt werden nur die Untertitel.
    Gibt None zurueck, wenn keine gefunden wurden — der Aufrufer entscheidet dann weiter.
    """
    import glob, json as _json, subprocess, tempfile, shutil as _shutil

    workdir = tempfile.mkdtemp(prefix="subs_")
    cookie_path = None
    try:
        langs = "de,en" if language in ("auto", "", None) else f"{language},de,en"
        cmd = [
            "yt-dlp", "--skip-download", "--no-playlist",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", langs,
            "--sub-format", "json3",
            "-o", os.path.join(workdir, "%(id)s.%(ext)s"),
        ]
        if cookies and cookies.strip():
            cookie_path = os.path.join(workdir, "cookies.txt")
            with open(cookie_path, "w") as cf:
                cf.write(cookies)
            cmd.extend(["--cookies", cookie_path])
        cmd.append(url)

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        files = sorted(glob.glob(os.path.join(workdir, "*.json3")))
        if not files:
            return {
                "found": False,
                "detail": _yt_error_detail(proc.stderr) if proc.returncode != 0 else
                          "yt-dlp hat keine Untertitel-Datei geschrieben",
            }

        # Bevorzugte Sprache zuerst, sonst die erste gefundene Datei.
        pick = next((f for f in files if f".{language}." in f), files[0])
        data = _json.load(open(pick, encoding="utf-8"))
        parts = []
        for ev in data.get("events", []):
            for seg in ev.get("segs") or []:
                t = seg.get("utf8", "")
                if t and t != "\n":
                    parts.append(t)
        text = " ".join(" ".join(parts).split())
        if not text:
            return {"found": False, "detail": "Untertitel-Datei war leer"}

        lang = os.path.basename(pick).split(".")[-2]
        return {"found": True, "text": text, "language": lang, "segments": len(data.get("events", []))}
    except subprocess.TimeoutExpired:
        return {"found": False, "detail": "yt-dlp Zeitueberschreitung beim Untertitel-Abruf (120s)"}
    except Exception as e:
        return {"found": False, "detail": f"{type(e).__name__}: {e}"}
    finally:
        _shutil.rmtree(workdir, ignore_errors=True)


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


@app.post("/youtube-transcript")
async def youtube_transcript(
    body: TranscriptRequest,
    _key: str = Security(require_api_key)
):
    """
    Holt YouTube-Untertitel direkt via youtube-transcript-api.
    Kein Download, kein Whisper — sofort verfuegbar.
    Fallback-Kette: gewuenschte Sprache → andere Sprache → auto-generated.
    """
    import re
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
    )

    url = body.url.strip()

    # Video-ID extrahieren
    match = re.search(r'(?:v=|youtu\.be/|/embed/|/v/)([a-zA-Z0-9_-]{11})', url)
    if not match:
        raise HTTPException(status_code=400, detail="Keine gueltige YouTube-URL / Video-ID")

    video_id = match.group(1)

    try:
        # Verfuegbare Transkripte auflisten.
        #
        # youtube-transcript-api hat in 1.0 den Einstiegspunkt umgestellt: die statische
        # Methode list_transcripts() gibt es nicht mehr, stattdessen die Instanzmethoden
        # list()/fetch(). Da die Abhaengigkeit in requirements.txt UNGEBUNDEN stand, hat ein
        # beliebiger Rebuild 1.x gezogen und diesen Endpunkt lautlos zerlegt: HTTP 200 mit
        # success:false und "type object 'YouTubeTranscriptApi' has no attribute
        # 'list_transcripts'". Am 2026-08-16 im Betrieb gemessen — 20 von 20 Videos ohne
        # Transkript, der Aufrufer wich still auf Web-Recherche aus.
        #
        # Beide Wege unterstuetzen, damit weder ein aelteres noch ein neueres Image bricht.
        # Alles UNTERHALB dieser Zeile ist unveraendert geblieben (find_manually_created_
        # transcript, find_generated_transcript, Iteration, is_generated) — nachgeprueft
        # gegen 1.2.4.
        if hasattr(YouTubeTranscriptApi, "list_transcripts"):
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)   # < 1.0
        else:
            # 1.x nimmt eine requests.Session entgegen (http_client). Damit lassen sich
            # dieselben Cookies verwenden wie beim Download — ohne sie antwortet YouTube
            # aus Rechenzentrums-Netzen mit einer Bot-Pruefung.
            transcript_list = YouTubeTranscriptApi(
                http_client=_session_with_cookies(body.cookies)
            ).list(video_id)                                                    # >= 1.0

        transcript = None
        used_language = None
        is_generated = False

        # Strategie 1: Manuelle Untertitel in gewuenschter Sprache
        preferred = [body.language] if body.language != "auto" else ["de", "en"]
        try:
            transcript = transcript_list.find_manually_created_transcript(preferred)
            used_language = transcript.language_code
            is_generated = False
        except NoTranscriptFound:
            pass

        # Strategie 2: Auto-generierte Untertitel in gewuenschter Sprache
        if not transcript:
            try:
                transcript = transcript_list.find_generated_transcript(preferred)
                used_language = transcript.language_code
                is_generated = True
            except NoTranscriptFound:
                pass

        # Strategie 3: Irgendein verfuegbares Transkript
        if not transcript:
            for t in transcript_list:
                transcript = t
                used_language = t.language_code
                is_generated = t.is_generated
                break

        if not transcript:
            return {
                "success": False,
                "has_transcript": False,
                "video_id": video_id,
                "message": "Keine Untertitel verfuegbar fuer dieses Video",
            }

        # Transkript-Daten holen
        entries = transcript.fetch()
        # Zu Fliesstext zusammenfuegen
        full_text = " ".join(e.get("text", e.text if hasattr(e, "text") else str(e)) if isinstance(e, dict) else e.text for e in entries)

        return {
            "success": True,
            "has_transcript": True,
            "video_id": video_id,
            "language": used_language,
            "is_generated": is_generated,
            "text": full_text,
            "segments": len(entries),
        }

    except TranscriptsDisabled:
        return {
            "success": False,
            "has_transcript": False,
            "video_id": video_id,
            "message": "Untertitel sind fuer dieses Video deaktiviert",
        }
    except VideoUnavailable:
        return {
            "success": False,
            "has_transcript": False,
            "video_id": video_id,
            "message": "Video nicht verfuegbar (privat, geloescht oder regional gesperrt)",
        }
    except Exception as e:
        # IP-Sperre ausdruecklich benennen. Sie ist in 1.x eine eigene Ausnahme
        # (RequestBlocked/IpBlocked), die es in aelteren Fassungen nicht gab — deshalb
        # ueber den Klassennamen erkannt statt ueber einen Import, der auf alten Images
        # scheitern wuerde.
        # Warum das wichtig ist: ohne diese Unterscheidung sieht "YouTube sperrt unsere
        # IP" in der Antwort genauso aus wie "dieses Video hat keine Untertitel". Der
        # erste Fall betrifft ALLE Videos und verlangt Cookies oder einen Proxy, der
        # zweite nur eines und ist normal. Am 2026-08-16 aus einem Rechenzentrums-Netz
        # reproduziert.
        if type(e).__name__ in ("RequestBlocked", "IpBlocked"):
            # Zweiter Weg: yt-dlp. Es spricht eine andere YouTube-Schnittstelle an und kommt
            # mit denselben Cookies durch, wo die Bibliothek blockiert wird — nachgemessen
            # am 2026-08-17 auf genau diesem Cluster.
            alt = _ytdlp_transcript(url, body.language, body.cookies)
            if alt and alt.get("found"):
                return {
                    "success": True,
                    "has_transcript": True,
                    "video_id": video_id,
                    "language": alt.get("language"),
                    "is_generated": True,   # ueber yt-dlp nicht sicher unterscheidbar
                    "text": alt["text"],
                    "segments": alt.get("segments", 0),
                    "via": "yt-dlp",
                }
            return {
                "success": False,
                "has_transcript": False,
                "blocked": True,
                "video_id": video_id,
                "message": "YouTube blockiert Anfragen von der IP dieses Clusters, und auch "
                           "der yt-dlp-Weg lieferte keine Untertitel. "
                           + str((alt or {}).get("detail", ""))[:250],
            }
        return {
            "success": False,
            "has_transcript": False,
            "video_id": video_id,
            "message": f"Fehler: {str(e)[:300]}",
        }


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
    cookie_path = None

    # Status setzen
    redis = request.app.state.redis
    await redis.set(f"job:{job_id}:status", "downloading")

    try:
        # Cookie-Datei schreiben wenn vorhanden
        if body.cookies and body.cookies.strip():
            cookie_path = os.path.join(UPLOAD_DIR, f"{job_id}_cookies.txt")
            with open(cookie_path, "w") as cf:
                cf.write(body.cookies)

        # yt-dlp Kommando bauen
        client = body.player_client if body.player_client is not None else "web"
        cmd = [
            "yt-dlp",
            "--no-playlist",
        ]
        if client:
            cmd.extend(["--extractor-args", f"youtube:player_client={client}"])
        fmt = body.format_selector if body.format_selector is not None else "bestaudio/best"
        if fmt:
            cmd.extend(["-f", fmt])
        cmd += [
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "-o", os.path.join(UPLOAD_DIR, f"{job_id}.%(ext)s"),
        ]
        if cookie_path:
            cmd.extend(["--cookies", cookie_path])
        cmd.append(url)

        # yt-dlp: Nur Audio extrahieren, bestes Format
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            # stderr NICHT von vorne abschneiden: yt-dlp stellt seiner Ausgabe eine
            # mehrzeilige Versionswarnung voran ("Your yt-dlp version ... is older than
            # 90 days"). Mit [:200] bestand die gespeicherte Fehlermeldung ausschliesslich
            # aus dieser Warnung — die eigentliche Ursache war NIE sichtbar.
            # Am 2026-08-16 beim Debuggen genau darauf gestossen: ein fehlgeschlagener Job
            # lieferte drei Zeilen Warnung und sonst nichts.
            detail = _yt_error_detail(result.stderr)
            await redis.set(f"job:{job_id}:error", detail)
            return {"job_id": job_id, "status": "failed", "error": detail}

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
        await redis.set(f"job:{job_id}:error", str(e)[:1500])
        return {"job_id": job_id, "status": "failed"}
    finally:
        # Cookie-Datei aufräumen
        if cookie_path:
            _remove_file(cookie_path)


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
