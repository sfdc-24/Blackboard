# Lou as code (lou_agent.py) as a long-running LiveKit Agents worker. Build context is the repo root, so
# the fleet's Redis client (scripts/redis_dual.py) rides along for T6; one copy of each file.
# Nothing here is a credential: the LiveKit keys, the OpenAI key, the prompt and the Redis auth arrive
# as Cloud Run secrets (cloud/lou-agent/deploy.sh). LOU_REDIS=off is the image default.
FROM python:3.12-slim

WORKDIR /app
COPY cloud/lou-agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/lou-agent/lou_tools.py /app/lou_tools.py
COPY cloud/lou-agent/lou_agent.py /app/lou_agent.py

ENV PYTHONUNBUFFERED=1
ENV LOU_REDIS=off

ENTRYPOINT ["python", "/app/lou_agent.py", "start"]
