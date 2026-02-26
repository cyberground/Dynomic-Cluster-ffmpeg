FROM linuxserver/ffmpeg:latest

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
  && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir arq redis

WORKDIR /app
COPY worker.py .

# linuxserver/ffmpeg hat eigenen ENTRYPOINT -> überschreiben
ENTRYPOINT ["/opt/venv/bin/python", "-m", "arq", "worker.WorkerSettings"]
