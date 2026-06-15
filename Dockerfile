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
    /app/data/conversations

COPY src/ ./src/
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV MCP_PORT=5000
ENV TOKENS_DIR=/app/data/tokens
ENV CONVERSATIONS_DIR=/app/data/conversations
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=4
ENV GUNICORN_TIMEOUT=180

EXPOSE 5000

CMD ["sh", "-c", "gunicorn \
  --bind 0.0.0.0:${MCP_PORT:-5000} \
  --workers ${GUNICORN_WORKERS:-2} \
  --threads ${GUNICORN_THREADS:-4} \
  --timeout ${GUNICORN_TIMEOUT:-180} \
  src.perplexity_mcp:app"]
