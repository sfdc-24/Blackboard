# Build context is the REPO ROOT; the scripts keep their layout under /app/scripts.
FROM python:3.12-slim

# No pip install: every module here is stdlib only.
WORKDIR /app
COPY scripts/bus.py scripts/wa_board_outbox.py scripts/wa_notify.py \
     scripts/state_store.py /app/scripts/
COPY cloud/wa-outbox/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# Given: BUS_URL, BUS_SECRET, META_TOKEN, WA_PHONE_NUMBER_ID, WA_TO,
# BLACKBOARD_STATE_URI. Not given: GH_TOKEN or any model key.
ENTRYPOINT ["python", "/app/main.py"]
