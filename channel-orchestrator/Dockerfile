FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY channel_orchestrator ./channel_orchestrator
COPY .env.example ./.env.example

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data

VOLUME ["/data"]

# default: HTTP API; override command for scheduler
CMD ["python", "-m", "channel_orchestrator.cli", "serve", "--host", "0.0.0.0", "--port", "8787"]
