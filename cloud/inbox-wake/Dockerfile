# Build context is the repo root, same as cloud/redis-tool-bridge, so the Redis
# client is scripts/redis_dual.py and not a second copy.
#
# INBOX_WAKE_ENABLED=off is the image default. A deploy that forgets the env
# var still wakes nobody. INBOX_WAKE_RECEIPTS=off is the receipt default:
# pickup writes nothing back until that variable is exactly "on".
# This image does not copy the board and does not write fleet:inbox:*.
# Scale-to-zero is a deploy flag (--min-instances=0), not something this
# image can turn on by itself.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "redis>=5,<6"

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/inbox-wake/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV INBOX_WAKE_ENABLED=off
ENV INBOX_WAKE_RECEIPTS=off

CMD ["python", "/app/main.py"]
