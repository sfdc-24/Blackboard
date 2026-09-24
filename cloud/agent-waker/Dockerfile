# Build context is the REPO ROOT. The files keep their repo layout under
# /app/scripts, because agent_waker.py posts through scripts/fleet_agent.py by
# path relative to its own parent - the same waker the laptop ran, unchanged.
FROM python:3.12-slim

# No pip install: every module here is stdlib only.
WORKDIR /app
COPY scripts/bus.py scripts/agent_waker.py scripts/gemini_agent.py \
     scripts/foundry_agent.py scripts/claude_agent.py \
     scripts/fleet_agent.py scripts/state_store.py /app/scripts/
COPY cloud/agent-waker/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# Given: BUS_URL, BUS_SECRET, BLACKBOARD_STATE_URI, AGENT, and ONLY that agent's
# credential (GEMINI_API_KEY | FOUNDRY_API_KEY + FOUNDRY_PROJECT_ENDPOINT |
# ANTHROPIC_API_KEY), set per job.
# Not given: META_TOKEN or GH_TOKEN. It answers board rows and nothing else.
ENTRYPOINT ["python", "/app/main.py"]
