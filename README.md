# FFmpeg Cluster

## Architektur

```
Client
  │
  ▼
nginx :9001  (Load Balancer, Round-Robin)
  │
  ├── api_1 (FastAPI)
  ├── api_2 (FastAPI)   ──► Redis Queue ──► worker_1 (FFmpeg, 2 Jobs parallel)
                        └──► worker_2 (FFmpeg, 2 Jobs parallel)
```

**Kapazität:** 3 API-Instanzen × 2 Worker × 2 parallele Jobs = **4 gleichzeitige Konvertierungen**

---

## Workflow

```
POST /mp4-to-mp3  →  { job_id: "abc-123", status: "queued" }
GET  /status/abc-123  →  { status: "processing" | "done" | "failed" }
GET  /download/abc-123  →  audio.mp3
```

---

## Starten

```bash
docker compose up -d --build
```

## Skalieren (Worker zur Laufzeit hinzufügen)

```bash
docker compose up -d --scale worker_1=1 --no-recreate
# oder einfach worker_3 in docker-compose.yml hinzufügen
```

## Logs

```bash
docker compose logs -f worker_1   # Worker-Logs
docker compose logs -f nginx      # Load Balancer
```

## Umgebungsvariablen

| Variable   | Default | Beschreibung                          |
|------------|---------|---------------------------------------|
| REDIS_HOST | redis   | Redis-Hostname                        |
| MAX_JOBS   | 1       | Gleichzeitige FFmpeg-Jobs pro Worker  |
