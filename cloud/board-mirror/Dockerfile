# Build context is the repo root, so the Redis client and the board reader are
# the shared copies (scripts/redis_dual.py, scripts/bus.py) and not a second
# implementation.
#
# BOARD_MIRROR_ENABLED=off is the image default. A deploy that forgets the
# variable still writes nothing. Rollback is that variable, not a new build.
# Scale-to-zero is a deploy flag (--min-instances=0), not something this
# image can turn on by itself. The service account is board-mirror@, not
# redis-tool-bridge and not inbox-wake.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "redis>=5,<6"

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY scripts/bus.py /app/bus.py
COPY cloud/board-mirror/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV BOARD_MIRROR_ENABLED=off

CMD ["python", "/app/main.py"]
