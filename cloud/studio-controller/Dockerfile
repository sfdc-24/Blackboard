FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv
COPY cloud/studio-controller/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY cloud/studio-controller/app ./app
# STUDIO_WORKER=claude imports workers.claude_worker (app/main.py); without
# this line that setting fails at startup with ModuleNotFoundError.
COPY cloud/studio-controller/workers ./workers
COPY scripts/state_store.py ./state_store.py

RUN useradd --create-home --uid 10001 studio && chown -R studio:studio /srv
USER studio

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
