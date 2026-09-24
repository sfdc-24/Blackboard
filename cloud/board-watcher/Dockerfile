# Build context is the REPO ROOT. The watcher imports the SAME addressing and
# outbox-matching code the jobs it starts use, so "is this row for gemini" has
# one answer on the whole fleet.
FROM python:3.12-slim

WORKDIR /app
COPY scripts/bus.py scripts/agent_waker.py scripts/wa_board_outbox.py \
     scripts/state_store.py /app/scripts/
COPY cloud/board-watcher/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# Given: BUS_URL, BUS_SECRET, BLACKBOARD_STATE_URI. It reads the board and starts
# jobs through the Run API with its metadata token. No model key, no Meta token.
ENTRYPOINT ["python", "/app/main.py"]
