# Build context is the repo root, like cloud/drive-intake: the Redis client is
# scripts/redis_dual.py and the contract (tuple, receipt grammar, heartbeat
# window) is cloud/drive-intake/main.py, copied in as drive_intake.py so there
# is one copy of each.
#
# CLAUDE_API_WORKER_ENABLED=off is the image default. A Cloud Run JOB, run on a
# Cloud Scheduler cadence (repeating work is a script on a timer): one pass,
# at most one model call, then exit. Model id, cap and reservation arrive as
# env; the key and the Redis auth as secrets. Nothing here is a credential.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "redis>=5,<6" tzdata

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/drive-intake/main.py /app/drive_intake.py
COPY cloud/claude-api-worker/gdocs.py /app/gdocs.py
COPY cloud/claude-api-worker/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV CLAUDE_API_WORKER_ENABLED=off

ENTRYPOINT ["python", "/app/main.py"]
