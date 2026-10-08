FROM python:3.12-slim

WORKDIR /app
COPY cloud/redis-tool-bridge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && python -c "from google.auth.transport.requests import Request; from google.oauth2 import id_token"

COPY scripts/redis_dual.py /app/redis_dual.py
COPY scripts/redis_dual.settings.json /app/redis_dual.settings.json
COPY cloud/redis-tool-bridge/main.py /app/main.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/main.py"]
