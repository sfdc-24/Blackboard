# Build context is the repo root, same as cloud/inbox-wake, so the Redis client
# is scripts/redis_dual.py and the ring is cloud/inbox-wake/main.py. Neither is
# copied into a second implementation.
#
# FLEET_DISPATCH=off is the image default. A deploy that forgets the variable
# still opens nothing. The pool's instance count is a deploy flag
# (--instances=1), not something this image can turn on by itself.
FROM python:3.12-slim

WORKDIR /app
# Pinned to the client cloud/redis-tool-bridge and cloud/bus-reconciler already
# resolved, so this image does not speak a different RESP than they do.
RUN pip install --no-cache-dir "redis==5.3.1"

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/inbox-wake/main.py /app/inbox_wake.py
COPY cloud/fleet-dispatch/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV FLEET_DISPATCH=off

CMD ["python", "/app/main.py"]
