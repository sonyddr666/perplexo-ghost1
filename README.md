# perplexo-ghost1

API Flask + bot Telegram para usar o Perplexity com scraper local embutido, pool de tokens, cookies complementares, historico por usuario, streaming SSE, upload de arquivos, visao por imagem e painel de credenciais.

## O Que Tem No Projeto

- API Flask em `src/perplexity_mcp.py`
- Bot Telegram em `src/telegram_bot.py`
- Gerenciador de tarefas do Telegram em `src/task_manager.py`
- TTS opcional em `src/tts_service.py`
- TokenManager em `src/token_manager.py`
- Scraper embutido em `src/perplexity_webui_scraper/`
- Pool de tokens com validacao, rotacao e refresh
- Cadastro de token/cookies por API, pagina web e Telegram
- Historico de conversas por `user_id`
- Busca JSON em `/search`
- Streaming SSE em `/search_stream`
- Upload de arquivos no `/search`
- Imagem base64 no `/vision`
- Pagina web de credenciais em `/credentials`
- Dockerfile e Docker Compose
- Porta padrao da API: `5000`

## Requisitos

- Python 3.12+
- Token de bot do Telegram, se for usar Telegram
- Token/cookies de sessao do Perplexity
- Docker opcional

Dependencias principais:

- Flask
- Gunicorn
- python-telegram-bot
- httpx
- APScheduler
- Flask-Limiter
- Pydantic
- Requests
- curl-cffi
- Loguru
- orjson
- Tenacity

## Estrutura

```text
perplexo-ghost1/
|-- src/
|   |-- perplexity_mcp.py
|   |-- telegram_bot.py
|   |-- task_manager.py
|   |-- token_manager.py
|   |-- tts_service.py
|   |-- templates/
|   `-- perplexity_webui_scraper/
|-- scripts/
|-- config/
|-- Dockerfile
|-- docker-compose.yml
|-- docker-entrypoint.sh
|-- requirements.txt
|-- .env.example
`-- README.md
```

## Configuracao

Crie um `.env` baseado no exemplo:

```bash
cp .env.example .env
```

Variaveis principais:

```env
MCP_PORT=5000
MCP_API_URL=http://127.0.0.1:5000
MCP_API_KEY=

PERPLEXITY_SESSION_TOKEN=
TOKEN_ROTATION_ENABLED=true
MAX_ACTIVE_CONVERSATIONS=100
CONVERSATION_TTL_SECONDS=3600

TELEGRAM_TOKEN=
TELEGRAM_PORT=8000
WEBHOOK_URL=
ALLOWED_TELEGRAM_USERS=
VPN_ENABLED=false
```

`PERPLEXITY_SESSION_TOKEN` pode ficar vazio se voce for cadastrar token/cookies pela pagina `/credentials`, pelos endpoints `/tokens/*` ou pelo Telegram.

`MCP_API_KEY` e opcional. Quando definida, os endpoints protegidos exigem:

```http
X-API-Key: sua_chave
```

## Rodar Com Docker

```bash
docker compose up -d --build
```

O container sobe a API Flask na porta `5000`. Se `TELEGRAM_TOKEN` estiver configurado, o mesmo container tambem inicia o bot Telegram. Se `TELEGRAM_TOKEN` estiver vazio, somente a API fica ativa.

## Rodar Sem Docker

```bash
pip install -r requirements.txt
python src/perplexity_mcp.py
```

Em outro terminal, para usar o Telegram:

```bash
python src/telegram_bot.py
```

## Telegram

O bot usa a mesma API Flask por `MCP_API_URL`. Cada conversa do Telegram usa o usuario do Telegram como identidade do historico.

Comandos principais:

- `/start` abre o painel principal
- `/modelos` troca o modelo
- `/busca` escolhe focus de busca
- `/tempo` escolhe filtro de tempo
- `/config` abre configuracoes
- `/new` cria nova conversa
- `/limpar` limpa contexto
- `/historico` lista historico
- `/token` abre o painel de tokens
- `/credencial`, `/credenciais` e `/credentials` abrem o mesmo painel de tokens
- `/tokencolar` permite colar o `__Secure-next-auth.session-token`
- `/colarcookies` permite colar o header `Cookie:`
- `/cookies` permite enviar JSON exportado do navegador
- `/apagartokens` limpa todos os tokens

Para restringir quem usa o bot, configure:

```env
ALLOWED_TELEGRAM_USERS=123456789,987654321
```

Se `WEBHOOK_URL` ficar vazio, o bot usa polling. Se `WEBHOOK_URL` estiver configurado, ele abre webhook em `/telegram` na porta `TELEGRAM_PORT`.

## Endpoints Principais

| Metodo | Rota | Descricao |
|---|---|---|
| `GET` | `/health` | Status geral da API |
| `GET` | `/models` | Lista modelos, focus modes e citation modes |
| `POST` | `/search` | Busca principal com resposta JSON |
| `POST` | `/search_stream` | Busca com streaming SSE |
| `POST` | `/vision` | Analise de imagem em base64 |
| `GET` | `/last_response` | Ultima resposta salva para um usuario |
| `POST` | `/clear` | Limpa a conversa ativa de um usuario |
| `GET` | `/conversation-status` | Status das conversas em memoria |
| `GET` | `/history/list` | Lista historicos salvos |
| `POST` | `/history/load` | Carrega historico salvo |
| `POST` | `/history/delete` | Remove historico salvo |
| `GET/POST` | `/config/library` | Consulta ou alterna salvar na biblioteca |
| `POST` | `/config/token` | Define token principal em runtime |
| `GET` | `/diagnostics` | Diagnostico de rede/autenticacao |

## Endpoints De Tokens

| Metodo | Rota | Descricao |
|---|---|---|
| `GET` | `/tokens` | Status do pool |
| `GET` | `/tokens/status` | Status do pool |
| `POST` | `/tokens/set` | Salva token, cookie string ou JSON de cookies |
| `GET/POST` | `/tokens/validate` | Valida token atual ou enviado |
| `POST` | `/tokens/rotate` | Alterna para outro token valido |
| `GET/POST` | `/tokens/pool` | Consulta ou adiciona conta ao pool |
| `GET/POST` | `/tokens/refresh` | Tenta renovar sessao |
| `POST` | `/tokens/upload_cookies` | Envia cookies complementares |
| `POST` | `/tokens/reload` | Recarrega pool do disco |
| `GET` | `/tokens/pool/status` | Status detalhado do pool |
| `POST` | `/tokens/pool/smart_refresh` | Refresh inteligente do pool |
| `POST` | `/tokens/pool/validate_all` | Valida todos os tokens |
| `POST` | `/tokens/pool/clear_invalid` | Limpa tokens invalidos |
| `POST` | `/tokens/clear_all` | Limpa tokens e runtime |

## Pagina De Credenciais

Abra:

```text
http://localhost:5000/credentials
```

Ela permite salvar token de sessao, cookie string, JSON exportado do navegador, testar credenciais e limpar credenciais locais.

## Exemplos de Uso

### Busca Mínima (modelo Best)

```bash
curl -s -X POST https://api.ghost1.cloud/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sua_chave" \
  -d "{\"query\":\"qual o melhor dia da semana?\"}"
```

### Busca com Modelo Sonar

```bash
curl -s -X POST https://api.ghost1.cloud/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sua_chave" \
  -d "{\"query\":\"qual o melhor dia da semana?\",\"model\":\"sonar\"}"
```

### Busca Completa (todos os parâmetros)

```bash
curl -s -X POST https://api.ghost1.cloud/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sua_chave" \
  -d "{\"query\":\"notícias de IA da semana\",\"user_id\":\"usuario-1\",\"model\":\"best\",\"focus\":\"web\",\"time_range\":\"week\",\"citation_mode\":\"markdown\"}"
```

### Parâmetros do `/search`

| Parâmetro | Padrão | Descrição | Opções |
|---|---|---|---|
| `query` | obrigatório | Sua pergunta ou busca | texto (1-10000 caracteres) |
| `model` | `best` | Modelo de IA | `best`, `sonar`, `deep-research`, `gpt-5.4`, `claude-sonnet-4.6` |
| `focus` | `web` | Foco da busca | `web`, `academic`, `social`, `finance`, `writing` |
| `time_range` | `all` | Período | `all`, `day`, `week`, `month`, `year` |
| `citation_mode` | `markdown` | Formato das citações | `default`, `markdown`, `clean` |
| `user_id` | `default` | Identificador do usuário | texto (max 100 caracteres) |

### Resposta

```json
{
  "status": "success",
  "answer": "Resposta com citações [1](url)...",
  "citations": [
    {"title": "...", "url": "...", "snippet": "..."}
  ],
  "conversation_info": {
    "id": "user_id",
    "message_count": 1,
    "model": "best",
    "uuid": "..."
  },
  "model_used": "best",
  "focus_mode": "web",
  "time_range": "all"
}
```

## Modelos

O endpoint `/models` retorna a lista real do scraper embutido. O projeto tambem aceita aliases amigaveis, incluindo:

- `best`
- `sonar`
- `deep-research`
- `gpt-5.4`
- `gpt-5.4-thinking`
- `gpt-5.2`
- `claude-sonnet-4.6`
- `claude-opus-4.6`
- `claude-4.6-opus`
- `gemini-3.1-pro`
- `gemini-3-flash`
- `grok-4.1`
- `kimi-k2.5-thinking`
- `nv-nemotron-3-super-thinking`

Tambem podem ser usados IDs canonicos retornados por `/models`, como:

```text
perplexity/best
perplexity/sonar-2
openai/gpt-5.4
anthropic/claude-sonnet-4.6
```

## Verificacao Rapida

```bash
python -m py_compile src/perplexity_mcp.py src/token_manager.py src/telegram_bot.py src/task_manager.py src/tts_service.py
python -m compileall -q src/perplexity_webui_scraper
```
