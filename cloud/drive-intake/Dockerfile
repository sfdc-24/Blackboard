# Build context is the repo root, the same as cloud/inbox-wake, so the Redis
# client is scripts/redis_dual.py and not a second copy. The Google Docs
# adapter is the fleet's one copy, kept beside the claude-api worker that also
# writes receipts; the poll feed is this service's own.
#
# DRIVE_INTAKE_ENABLED=off is the image default. A deploy that forgets the env
# var reads no doc, claims nothing and writes no receipt. Scale-to-zero is a
# deploy flag (--min-instances=0), not something this image can turn on.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "redis>=5,<6"

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/claude-api-worker/gdocs.py /app/gdocs.py
COPY cloud/drive-intake/drive_feed.py /app/drive_feed.py
COPY cloud/drive-intake/drive_watch.py /app/drive_watch.py
COPY cloud/drive-intake/task_filter.py /app/task_filter.py
COPY cloud/drive-intake/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV DRIVE_INTAKE_ENABLED=off

CMD ["python", "/app/main.py"]
