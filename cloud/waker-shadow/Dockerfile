# Build context is the REPO ROOT so board_waker.py is the ONE copy of the
# decision logic. A second copy would make this shadow prove that a fork of the
# waker agrees with itself, which is worth nothing - the whole point is that the
# code the laptop runs reaches the same verdict in a container.
#
# What reaches the build is controlled by /.gcloudignore, an allowlist. The repo
# is 332 MB and holds a .env with 31 live credentials; neither goes up.
FROM python:3.12-slim

# No pip install. board_waker, board_say, bus and state_store are stdlib only.
WORKDIR /app
COPY scripts/bus.py /app/bus.py
COPY scripts/board_say.py /app/board_say.py
COPY scripts/board_waker.py /app/board_waker.py
COPY scripts/state_store.py /app/state_store.py
COPY cloud/waker-shadow/main.py /app/main.py

# Unbuffered so a job killed mid-run still leaves its verdict in Cloud Logging.
ENV PYTHONUNBUFFERED=1

# WHAT IS DELIBERATELY NOT HERE, and it is the security argument rather than a
# note: no META_TOKEN and no GH_TOKEN are injected at deploy time, so this
# container cannot send Mr Salam a WhatsApp message or touch a repository even if
# some future edit tried to. Writes are disabled by absence of capability, not by
# a flag someone can flip. The job's output records both as false so a reviewer
# can see it without going to look at the environment.
ENTRYPOINT ["python", "/app/main.py"]
