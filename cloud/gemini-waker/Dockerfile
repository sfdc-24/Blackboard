# Build context is the REPO ROOT. The files keep their repo layout under
# /app/scripts, because agent_waker.py posts through scripts/fleet_agent.py by
# path relative to its own parent - the same waker the laptop ran, unchanged.
FROM python:3.12-slim

# No pip install: every module here is stdlib only.
WORKDIR /app
COPY scripts/bus.py scripts/agent_waker.py scripts/gemini_agent.py \
     scripts/fleet_agent.py scripts/state_store.py /app/scripts/
COPY cloud/gemini-waker/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# Given: BUS_URL, BUS_SECRET, GEMINI_API_KEY, BLACKBOARD_STATE_URI.
# Not given: META_TOKEN or GH_TOKEN. It answers board rows and nothing else.
ENTRYPOINT ["python", "/app/main.py"]
