# Build context is THIS directory: the watcher is one stdlib-only file, and nothing else from the
# repo (least of all .env) belongs in the image. The job's credentials arrive as Secret Manager
# bindings at deploy time; nothing is baked here.
FROM python:3.12-slim
WORKDIR /app
COPY watcher.py /app/watcher.py
# Unbuffered and bytecode-free, as the fleet's other jobs run: every say() line reaches the log
# the moment it happens, because the restore evidence must not sit in a buffer if the job is killed.
ENTRYPOINT ["python", "-B", "-u", "/app/watcher.py"]
