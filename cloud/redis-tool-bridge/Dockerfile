# Build context is the REPO ROOT, same shape and same reason as cloud/bus-reconciler/Dockerfile:
# the connection path must be the ONE copy in scripts/redis_dual.py. A private copy of the Redis
# client in here is how two processes come to disagree about whether TLS verification is on, and
# "server identity verification was quietly disabled" is literally defect 2 of the review this
# service is being rewritten to answer.
#
# What reaches the build is controlled by /.gcloudignore, which is an ALLOWLIST. The repo holds a
# .env with live credentials and it does not go up. Run `gcloud meta list-files-for-upload` before
# the first submit and read the list - an allowlist you have not listed is a guess.
FROM python:3.12-slim

WORKDIR /app
COPY cloud/redis-tool-bridge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The connection, the off-switch and the TLS rules - borrowed, never reimplemented.
COPY scripts/redis_dual.py /app/redis_dual.py
# The switches themselves. It ships enabled=false and connect=true, holds no address and no
# credential, and is re-read on every call. Its ABSENCE stops the service dead, correctly: an
# unreadable settings file vetoes both switches rather than defaulting them on.
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/redis-tool-bridge/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

# NO SECRET AND NO ADDRESS IS BAKED IN. Given at deploy time, and the two secrets arrive as FILE
# MOUNTS in separate directories, never as environment variables - defect 4, and also what the
# already-running revision does, so this is not a regression of it:
#     REDIS_HOST, REDIS_PORT
#     REDIS_AUTH_FILE      /secrets/auth/redis-auth
#     REDIS_CA_CERT_PATH   /secrets/ca/redis-ca.pem
#     BRIDGE_AUDIENCE      the service's own https URL; without it every request is refused
#     BRIDGE_CALLERS       comma-separated service-account emails; without it every request is refused
CMD ["python", "/app/main.py"]
