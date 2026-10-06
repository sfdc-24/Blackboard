# Build context is the REPO ROOT, not this directory, so that scripts/bus.py is
# the one and only copy of the read path. A second copy of bus.py in here is
# exactly how a reader and a writer come to disagree about what the board says -
# which is the whole class of bug this reconciler exists to detect, so shipping
# its own private copy would be self-defeating.
#
# What reaches the build is controlled by /.gcloudignore, which is an ALLOWLIST:
# these four files and nothing else. The repo is 332 MB and holds a .env with
# live credentials; neither goes up. Verified with
# `gcloud meta list-files-for-upload` before the first submit.
FROM python:3.12-slim

WORKDIR /app
COPY cloud/bus-reconciler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The read path, unmodified and unduplicated.
COPY scripts/bus.py /app/bus.py
# The dual-run wrapper: the connection, the off-switch, and the rule that the
# authoritative answer always wins. The reconciler borrows its client and its
# settings rather than opening a second connection with different rules.
COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/bus_reconcile.py /app/bus_reconcile.py
COPY cloud/bus-reconciler/main.py /app/main.py
# The connectivity probe. One key under its own prefix, read back, deleted. It exists so that
# "does the pipe work" can be answered without running the reconciler, which is under hold.
COPY cloud/bus-reconciler/probe.py /app/probe.py

# No credential in the image. BUS_URL, BUS_SECRET and the Redis AUTH string all
# arrive at runtime - the first two as secret-backed environment variables, the
# third read from Secret Manager by redis_dual at connect time.
ENTRYPOINT ["python", "-B", "/app/main.py"]
