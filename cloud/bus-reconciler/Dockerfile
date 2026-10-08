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
# THE SWITCHES THEMSELVES. This file had never travelled into the image, and under the old code
# nobody noticed: a missing settings file read as {} and REDIS_DUAL_ENABLED in the environment
# turned the dual-run on anyway. Now that an unreadable file vetoes both switches - which is the
# fix to the off-switch - its absence stops the job dead, correctly. It ships enabled=false and
# connect=true, holds no address and no credential, and is re-read on every call, so editing it
# stops the dual-run with no redeploy.
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY scripts/bus_reconcile.py /app/bus_reconcile.py
COPY cloud/bus-reconciler/main.py /app/main.py
# The connectivity probe. One key under its own prefix, read back, deleted. It exists so that
# "does the pipe work" can be answered without running the reconciler, which is under hold.
COPY cloud/bus-reconciler/probe.py /app/probe.py
# The request-handling mode and its entrypoint. append.py comes with them because it is the
# ONE board-write path on this fleet that sends a single transport attempt and then reads the
# row back by Row_ID; a second implementation in here would be a second thing to get wrong.
COPY scripts/bus_request.py /app/bus_request.py
# Governed Redis access (redis-op): the module that decides, and the access list it decides by.
# The ACL is baked in, so changing anyone's rights is a reviewed commit and a rebuild - and every
# audit entry names the ACL digest it was decided under.
COPY scripts/redis_gov.py /app/redis_gov.py
COPY scripts/redis_acl.json /app/redis_acl.json
# The git channel: Redis requests from GitHub bot accounts the access list binds by numeric id (Cursor).
COPY scripts/git_requests.py /app/git_requests.py
COPY scripts/append.py /app/append.py
# append.py imports signature: a row must name the INSTANCE that wrote it and how it got here,
# checked at the writer. WITHOUT THIS LINE THE IMAGE DIES ON IMPORT - the request worker's only
# board-write path would fail to load, which is the merge-order hazard the guard's own commit
# warned about, reappearing inside the container. The roster it reads is already copied below.
COPY scripts/signature.py /app/signature.py
COPY cloud/bus-reconciler/serve_requests.py /app/serve_requests.py
# The roster: the file is the ORIGINAL, Redis gets a copy. Both travel so the seeder can verify
# the copy against the original from inside the VPC, which is the only place either is reachable.
COPY scripts/agent_roster.json /app/agent_roster.json
COPY scripts/roster_seed.py /app/roster_seed.py
# The keyspace viewer. redis-central has a private address and the Cloud console shows the
# instance but never its contents, so the only way a person sees what is in there is a job
# inside the VPC that prints it. Read-only by construction: it refuses any command outside
# its permitted set before sending it. NOT named inspect.py - that shadows the standard
# library module the redis client imports.
COPY cloud/bus-reconciler/keyspace_view.py /app/keyspace_view.py

# No credential in the image. BUS_URL, BUS_SECRET and the Redis AUTH string all
# arrive at runtime - the first two as secret-backed environment variables, the
# third read from Secret Manager by redis_dual at connect time.
ENTRYPOINT ["python", "-B", "/app/main.py"]
