#!/bin/sh
set -e

echo "Iniciando perplexo-ghost1..."

gunicorn \
  --bind "0.0.0.0:${MCP_PORT:-5000}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-180}" \
  src.perplexity_mcp:app &
MCP_PID="$!"

MCP_URL="http://127.0.0.1:${MCP_PORT:-5000}/health"
echo "Aguardando API Flask em ${MCP_URL}..."

retry=0
until python -c "import urllib.request; urllib.request.urlopen('${MCP_URL}', timeout=2)" 2>/dev/null; do
  retry=$((retry + 1))
  if [ "$retry" -ge 15 ]; then
    echo "API Flask nao respondeu. Encerrando."
    kill "$MCP_PID" 2>/dev/null || true
    wait "$MCP_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 2
done

if [ -z "${TELEGRAM_TOKEN:-}" ] || [ "${TELEGRAM_TOKEN:-}" = "seu_token_aqui" ]; then
  echo "TELEGRAM_TOKEN nao configurado. Mantendo somente a API Flask ativa."
  wait "$MCP_PID"
else
  echo "Iniciando bot Telegram..."
  exec python src/telegram_bot.py
fi
