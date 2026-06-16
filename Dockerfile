# =====================
# Perplexo API - Python
# =====================

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p \
    /app/data/tokens \
    /app/data/conversations \
    /app/data/tasks

COPY src/ ./src/
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV MCP_PORT=5000
ENV MCP_API_URL=http://127.0.0.1:5000
ENV TELEGRAM_PORT=8000
ENV TOKENS_DIR=/app/data/tokens
ENV CONVERSATIONS_DIR=/app/data/conversations
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=4
ENV GUNICORN_TIMEOUT=180

EXPOSE 5000 8000

COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

CMD ["./docker-entrypoint.sh"]
