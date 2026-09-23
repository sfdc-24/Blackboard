# Build context is the REPO ROOT, not this directory, so that scripts/bus.py is
# the one and only copy of the read path. A second copy of bus.py in here is
# exactly how a reader and a writer come to disagree about what the board says -
# the class of bug this probe exists to detect.
#
# What reaches the build is controlled by /.gcloudignore, which is an ALLOWLIST:
# these two files and nothing else. The repo is 332 MB and holds a .env with 31
# live credentials; neither goes up.
FROM python:3.12-slim

# No pip install. The probe and bus.py are stdlib only, deliberately - an import
# that is absent in a sandbox is the dependency this whole migration exists to
# remove, and a requirements.txt here would quietly re-introduce one.
WORKDIR /app
COPY scripts/bus.py /app/bus.py
# board_say holds bus_get, the query-string read transport. bus.py comes with
# it because board_say imports load_env from it.
COPY scripts/board_say.py /app/board_say.py
COPY cloud/board-probe/main.py /app/main.py

# Unbuffered so a Cloud Run Job that is killed mid-run still leaves its output in
# Cloud Logging rather than losing it in a pipe.
ENV PYTHONUNBUFFERED=1

# No shell form, so the process gets signals directly and there is no /bin/sh
# interpreting anything.
ENTRYPOINT ["python", "/app/main.py"]
