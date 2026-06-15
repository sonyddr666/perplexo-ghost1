# perplexo-ghost1

API REST em Flask para usar Perplexity por HTTP, com gerenciamento local de tokens, historico por usuario, streaming SSE, suporte a arquivos/imagens e scraper embutido no proprio projeto.

## O Que Este Projeto Tem

- API Flask em `src/perplexity_mcp.py`
- Gerenciador de tokens em `src/token_manager.py`
- Scraper Perplexity embutido em `src/perplexity_webui_scraper/`
- Pool de tokens com validacao, rotacao e refresh
- Historico de conversas por `user_id`
- Busca normal em JSON
- Busca com streaming via SSE
- Upload de arquivos no endpoint `/search`
- Analise de imagem/base64 no endpoint `/vision`
- Pagina local de credenciais em `/credentials`
- Dockerfile e Docker Compose para deploy
- Autenticacao opcional por `X-API-Key`

## Requisitos

- Python 3.12 ou superior
- Token de sessao do Perplexity
- Docker opcional para deploy em container

Dependencias Python principais:

- Flask
- Gunicorn
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
├── src/
│   ├── perplexity_mcp.py
│   ├── token_manager.py
│   ├── templates/
│   └── perplexity_webui_scraper/
├── scripts/
├── config/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

## Configuracao

Crie um `.env` a partir do exemplo:

```bash
cp .env.example .env
```

Variaveis principais:

```env
PERPLEXITY_SESSION_TOKEN=
MCP_API_KEY=
FLASK_ENV=production
MCP_PORT=5000
TOKENS_DIR=./data/tokens
CONVERSATIONS_DIR=./data/conversations
MAX_ACTIVE_CONVERSATIONS=100
CONVERSATION_TTL_SECONDS=3600
TOKEN_ROTATION_ENABLED=true
```

`PERPLEXITY_SESSION_TOKEN` pode ficar vazio se voce for cadastrar tokens pela pagina `/credentials` ou pelos endpoints `/tokens/*`.

`MCP_API_KEY` e opcional. Quando definida, os endpoints protegidos exigem o header:

```http
X-API-Key: sua_chave
```

## Rodar Com Docker

```bash
docker compose up -d --build
```

Por padrao, o container expoe a API na porta definida em `MCP_PORT`.

Healthcheck:

```text
GET /health
```

## Rodar Sem Docker

```bash
pip install -r requirements.txt
python src/perplexity_mcp.py
```

A API sobe na porta configurada em `MCP_PORT`.

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
| `GET/POST` | `/config/library` | Consulta ou alterna salvar na biblioteca Perplexity |
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

Ela permite:

- salvar token de sessao
- salvar cookie string
- salvar JSON exportado do navegador
- testar credenciais
- limpar credenciais locais

Se `MCP_API_KEY` estiver configurada, a pagina pede a chave para executar acoes administrativas.

## Exemplo De Busca

```bash
curl -X POST http://localhost:5000/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sua_chave" \
  -d '{
    "query": "Resuma as principais noticias de IA da semana",
    "user_id": "usuario-1",
    "model": "best",
    "focus": "web",
    "time_range": "week",
    "citation_mode": "markdown"
  }'
```

Resposta:

```json
{
  "status": "success",
  "answer": "...",
  "thinking": null,
  "model_used": "best",
  "focus_mode": "web",
  "time_range": "week",
  "citations": [],
  "has_thinking": false,
  "conversation_info": {
    "id": "usuario-1",
    "uuid": null,
    "model": "best",
    "message_count": 1
  }
}
```

## Modelos

O endpoint `/models` retorna a lista de modelos disponiveis no scraper embutido.

Aliases aceitos no `/search` incluem:

- `best`
- `sonar`
- `deep-research`
- `gpt-5.4`
- `gpt-5.4-thinking`
- `claude-sonnet-4.6`
- `claude-sonnet-4.6-thinking`
- `claude-opus-4.7`
- `claude-opus-4.7-thinking`
- `gemini-3.1-pro`
- `kimi-k2.6-instant`
- `kimi-k2.6-thinking`
- `nv-nemotron-3-super-thinking`

Tambem podem ser usados IDs canonicos retornados por `/models`, como:

```text
perplexity/best
perplexity/sonar-2
openai/gpt-5.4
anthropic/claude-sonnet-4.6
```

## Focus E Filtros

`focus` aceitos:

- `web`
- `academic`
- `social`
- `finance`
- `writing`

`time_range` aceitos:

- `all`
- `day`
- `week`
- `month`
- `year`

`citation_mode` aceitos:

- `default`
- `markdown`
- `clean`

## Streaming SSE

```bash
curl -N -X POST http://localhost:5000/search_stream \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sua_chave" \
  -d '{
    "query": "Explique MCP de forma pratica",
    "user_id": "stream-demo",
    "model": "best",
    "focus": "web"
  }'
```

Eventos emitidos:

- `status`
- `thinking`
- `citation`
- `chunk`
- `file`
- `clarifying_question`
- `done`
- `error`

## Upload De Arquivos

`/search` aceita `multipart/form-data`:

```bash
curl -X POST http://localhost:5000/search \
  -H "X-API-Key: sua_chave" \
  -F "query=Analise este arquivo" \
  -F "user_id=arquivo-demo" \
  -F "model=best" \
  -F "focus=web" \
  -F "file=@./arquivo.pdf"
```

## Vision

```bash
curl -X POST http://localhost:5000/vision \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sua_chave" \
  -d '{
    "query": "Descreva a imagem",
    "image_base64": "BASE64_AQUI",
    "model": "best"
  }'
```

## Historico

A API mantem uma conversa ativa por `user_id`.

Use:

```text
POST /clear
GET  /history/list
POST /history/load
POST /history/delete
```

O diretorio de historico e definido por:

```env
CONVERSATIONS_DIR=./data/conversations
```

## Salvar Na Biblioteca Do Perplexity

Por padrao, as conversas nao sao salvas na biblioteca remota da conta.

Para consultar:

```bash
curl http://localhost:5000/config/library
```

Para alternar:

```bash
curl -X POST http://localhost:5000/config/library \
  -H "X-API-Key: sua_chave"
```

## Diagnostico

```bash
curl http://localhost:5000/diagnostics
```

Retorna informacoes sobre:

- status da API
- IP publico detectado
- disponibilidade do scraper
- cliente Perplexity configurado

## Problemas Comuns

`503 Cliente nao inicializado`

Configure um token em `PERPLEXITY_SESSION_TOKEN`, use `/credentials` ou envie token por `/tokens/set`.

`401 Unauthorized`

Confira o header `X-API-Key`.

`403` ou erro de autenticacao no Perplexity

Atualize o token/cookies e use os endpoints de refresh/validacao do pool.

Streaming nao aparece

Confirme que o cliente HTTP aceita `text/event-stream` e nao esta bufferizando a resposta.
