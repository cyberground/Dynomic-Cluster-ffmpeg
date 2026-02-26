# FFmpeg Cluster – Dokumentation

## Was haben wir gebaut?

Ein skalierbarer FFmpeg-Dienst mit Queue-System, bestehend aus 6 Komponenten:

**nginx** – Load Balancer, der einzige öffentlich erreichbare Service (Port 9001).
Verteilt eingehende Requests per Round-Robin auf die API-Instanzen.

**api_1 / api_2 / api_3** – FastAPI-Instanzen (Python).
Nehmen Datei-Uploads entgegen, legen Jobs in die Redis-Queue und antworten
sofort mit einer `job_id`. Kein Warten auf FFmpeg.

**worker_1 / worker_2** – FFmpeg-Worker (linuxserver/ffmpeg + arq).
Holen Jobs aus der Queue und führen die eigentliche Konvertierung durch.
Jeder Worker kann 2 Jobs gleichzeitig verarbeiten → 4 parallele Konvertierungen.

**redis** – Queue + Status-Speicher.
Vermittelt zwischen API und Workern, speichert Job-Status (queued / processing / done / failed).

---

## API-Endpunkte

| Methode | Endpunkt              | Beschreibung                                 |
|---------|-----------------------|----------------------------------------------|
| GET     | /health               | Healthcheck                                  |
| POST    | /mp4-to-mp3           | Datei hochladen → gibt `job_id` zurück       |
| GET     | /status/{job_id}      | Status abfragen (queued/processing/done)     |
| GET     | /download/{job_id}    | Fertige MP3 herunterladen                    |

---

## Git Repository

**URL:** https://github.com/cyberground/Dynomic-Cluster-ffmpeg  
**Branch:** main

### Projektstruktur

```
ffmpeg-cluster/
├── docker-compose.yml
├── api/
│   ├── Dockerfile
│   └── app.py
├── worker/
│   ├── Dockerfile
│   └── worker.py
└── nginx/
    ├── Dockerfile
    └── nginx.conf
```

---

## Push-Befehl für nächste Session

Dateien lokal anpassen, dann:

```bash
cd /home/claude/ffmpeg-cluster
git add .
git commit -m "Update"
git remote set-url origin https://TOKEN@github.com/cyberground/Dynomic-Cluster-ffmpeg.git
git push
```

> Token ersetzen durch aktuellen GitHub Personal Access Token (repo-Berechtigung).
> Neuen Token erstellen unter: GitHub → Settings → Developer Settings → Tokens (classic)

---

## Deployment (Coolify)

- **Build Pack:** Docker Compose
- **Repo URL:** https://github.com/cyberground/Dynomic-Cluster-ffmpeg
- **Branch:** main
- **Docker Compose Location:** /docker-compose.yml
- **Domain:** nur für nginx vergeben, alle anderen Services leer lassen
- Nach Code-Änderungen: einfach pushen → in Coolify **Deploy** klicken

---

## Skalierung

Mehr Kapazität braucht nur eine Änderung in der `docker-compose.yml`:
`worker_3`, `worker_4` usw. hinzufügen – kein Code-Änderung nötig.
