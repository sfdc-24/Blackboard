# Build context is the REPO ROOT. The files keep their repo layout under
# /app/scripts, because agent_waker.py posts through scripts/fleet_agent.py by
# path relative to its own parent - the same waker the laptop ran, unchanged.
FROM python:3.12-slim

# No pip install: every module here is stdlib only.
WORKDIR /app
COPY scripts/bus.py scripts/agent_waker.py scripts/gemini_agent.py \
     scripts/claude_agent.py scripts/repo_context.py scripts/okf_land.py \
     scripts/fleet_agent.py scripts/state_store.py /app/scripts/
COPY cloud/agent-waker/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# Given: BUS_URL, BUS_SECRET, BLACKBOARD_STATE_URI, AGENT, and ONLY that agent's
# credential (GEMINI_API_KEY | ANTHROPIC_API_KEY), set per job. The Gemini job
# alone may also get GEMINI_GITHUB_TOKEN, a fine-grained token with "Public
# repositories (read-only)" access that scripts/repo_context.py uses to read the
# public PRs a row names (the owner approved it, 2026-09-29). It can read no
# private repository, whatever a row asks.
# For signed OKF RESULT files, may also get GEMINI_OKF_WRITE_TOKEN (Blackboard
# Contents:Write + Pull requests:Write only). scripts/okf_land.py lands under
# docs/okf/gemini/ and opens a PR; never auto-merges.
# Not given: META_TOKEN or a broad GH_TOKEN. It answers board rows and may land OKF.
ENTRYPOINT ["python", "/app/main.py"]
