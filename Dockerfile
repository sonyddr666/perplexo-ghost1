# =====================
# Perplexo API - Python
# =====================
# API REST para integração com skills e clientes HTTP

FROM python:3.12-slim

WORKDIR /app

# Dependências do sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements
COPY requirements.txt .

# Instala dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Cria diretórios de dados
RUN mkdir -p \
    /app/data/tokens \
    /app/data/conversations

# Copia código fonte
COPY src/ ./src/

# Copia scripts de refresh
COPY scripts/ ./scripts/

# Tokens são injetados via variáveis de ambiente no runtime (Coolify)

# Variáveis de ambiente
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV MCP_PORT=3000
ENV TOKENS_DIR=/app/data/tokens
ENV CONVERSATIONS_DIR=/app/data/conversations
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=4
ENV GUNICORN_TIMEOUT=180

EXPOSE 3000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${MCP_PORT:-3000} --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} --timeout ${GUNICORN_TIMEOUT:-180} src.perplexity_mcp:app"]
