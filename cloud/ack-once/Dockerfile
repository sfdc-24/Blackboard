# Build context is the REPO ROOT so board_say.py is the ONE copy of the write
# path. A second copy of the append logic would be a second thing to get wrong on
# an append-only board, where a malformed row cannot be taken back.
FROM python:3.12-slim

# No pip install. board_say, bus and state_store are stdlib only.
WORKDIR /app
COPY scripts/bus.py /app/bus.py
COPY scripts/board_say.py /app/board_say.py
COPY scripts/state_store.py /app/state_store.py
COPY cloud/ack-once/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# THIS IS THE ONE CLOUD JOB THAT MAY WRITE TO THE BOARD, so what it is NOT given
# matters as much as what it is: no META_TOKEN, so it cannot message Mr Salam; no
# GH_TOKEN, so it cannot touch a repository. It writes exactly one board row,
# bounded by an allowlist that refuses everything when unconfigured, and it
# requires BLACKBOARD_STATE_URI - without durable state it cannot promise it has
# not already run, so it refuses rather than risk a duplicate.
ENTRYPOINT ["python", "/app/main.py"]
