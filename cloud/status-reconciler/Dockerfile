# Build context is the REPO ROOT, not this directory, so that scripts/board_say.py,
# scripts/bus.py, scripts/rstatus_reconcile.py, scripts/open_for_me.py, scripts/ask_phases.py
# and scripts/status_client.py are the one and only copies of the read/compute/write path.
# A second copy of any of them in here is exactly how a reader and a writer come to
# disagree about what the board says, which is the whole class of bug this reconciler
# exists to close (docs/BOARD-STATUS-FRAMEWORK.md).
#
# What reaches the build is controlled by /.gcloudignore, which is an ALLOWLIST: these
# seven files and nothing else. The repo is 332 MB and holds a .env with live
# credentials; neither goes up. Verified with `gcloud meta list-files-for-upload`
# before the first submit, same discipline as cloud/board-probe and cloud/bus-reconciler.
FROM python:3.12-slim

# No pip install. Every file this job imports is stdlib only, deliberately - an
# import that is absent in a sandbox is the dependency this whole migration exists
# to remove, and a requirements.txt here would quietly re-introduce one.
WORKDIR /app
COPY scripts/bus.py /app/bus.py
COPY scripts/board_say.py /app/board_say.py
# The HOLD lifecycle (ACTIVE/CLEARED/SUPERSEDED_LIKELY) and the BCB-1 board loader -
# reused here, not reimplemented (blackboard-5e's ruling, 2026-11-14).
COPY scripts/open_for_me.py /app/open_for_me.py
# The one ASK_PHASES constant every writer, the lint rule and this reconciler share
# (Gemini's ruling, 2026-11-14: ASK, REQUEST, DISPATCH, REVIEW_REQUEST, TASK).
COPY scripts/ask_phases.py /app/ask_phases.py
# The pure state machine: board rows -> Status columns M to R.
COPY scripts/rstatus_reconcile.py /app/rstatus_reconcile.py
# The gas-bus status_write/status_setup client - the ONLY path this job writes
# through; no Sheets API credential lives in this image.
COPY scripts/status_client.py /app/status_client.py
COPY cloud/status-reconciler/main.py /app/main.py

# Unbuffered so a Cloud Run Job that is killed mid-run still leaves its output in
# Cloud Logging rather than losing it in a pipe.
ENV PYTHONUNBUFFERED=1

# No shell form, so the process gets signals directly and there is no /bin/sh
# interpreting anything.
ENTRYPOINT ["python", "/app/main.py"]
