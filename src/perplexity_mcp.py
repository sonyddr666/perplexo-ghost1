"""
Perplexity MCP Server
=====================
API Flask que integra com o scraper real do Perplexity.ai
usando perplexity-webui-scraper.

Endpoints:
- POST /search   - Busca com modelo/focus configurável
- POST /vision   - Análise de imagens/arquivos
- GET  /models   - Lista modelos e focus modes disponíveis
- GET  /health   - Health check

Uso:
    pip install git+https://github.com/henrique-coder/perplexity-webui-scraper
    python src/perplexity_mcp.py
"""

import os
import sys
import base64
import tempfile
import logging
import traceback
import json
import uuid
import time
import threading
import functools
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

# Garante que o pacote local (src/perplexity_webui_scraper) tem prioridade
_src_dir = str(Path(__file__).resolve().parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Carrega .env
load_dotenv()

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============= CONFIGURAÇÃO =============

app = Flask(__name__)

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["100 per hour", "10 per minute"]
)

# Session token do Perplexity
PERPLEXITY_SESSION_TOKEN = os.getenv("PERPLEXITY_SESSION_TOKEN", "")
MCP_PORT = int(os.getenv("MCP_PORT", 5000))

# API Key para autenticação do MCP Server
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

# Limites de memória
MAX_ACTIVE_CONVERSATIONS = int(os.getenv("MAX_ACTIVE_CONVERSATIONS", 100))
CONVERSATION_TTL_SECONDS = int(os.getenv("CONVERSATION_TTL_SECONDS", 3600))  # 1 hora


# ============= PYDANTIC MODELS =============

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=10000)
    user_id: str = Field(default="default", max_length=100)
    model: str = Field(default="best", max_length=50)
    focus: str = Field(default="web", max_length=30)
    time_range: str = Field(default="all", max_length=20)
    citation_mode: str = Field(default="markdown", max_length=20)


class VisionRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000)
    image_base64: str = Field(..., min_length=10)
    model: str = Field(default="best", max_length=50)


class ClearRequest(BaseModel):
    user_id: str = Field(default="default", max_length=100)


class TokenUpdateRequest(BaseModel):
    token: str = Field(..., min_length=10)


# ============= API KEY MIDDLEWARE =============

def require_api_key(f):
    """Decorator para exigir API key nos endpoints protegidos"""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not MCP_API_KEY:
            # Se não tem API key configurada, aceita qualquer request (dev mode)
            return f(*args, **kwargs)
        
        # Verifica header X-API-Key
        provided_key = request.headers.get('X-API-Key', '')
        if not provided_key:
            # Fallback: query param
            provided_key = request.args.get('api_key', '')
        
        if provided_key != MCP_API_KEY:
            return jsonify({"error": "Unauthorized", "message": "API key inválida ou ausente"}), 401
        
        return f(*args, **kwargs)
    return decorated

# ============= TOKEN MANAGER =============
from token_manager import get_token_manager, TokenManager, _start_auto_refresh_worker, detect_input_type

try:
    token_manager = get_token_manager()
    logger.info(f"🔑 TokenManager v3: {token_manager.get_pool_status()['total']} token(s) no pool")
    # Inicia worker de auto-refresh (6h)
    _start_auto_refresh_worker(get_token_manager)
except Exception as e:
    logger.warning(f"⚠️ TokenManager não disponível: {e}")
    token_manager = None

# ============= IMPORTAÇÃO DO SCRAPER REAL =============

SCRAPER_AVAILABLE = False
Perplexity = None
ConversationConfig = None
MODELS = None
SourceFocus = None
CitationMode = None
TimeRange = None
Coordinates = None

try:
    from perplexity_webui_scraper import Perplexity as _Perplexity
    from perplexity_webui_scraper import ConversationConfig as _ConversationConfig
    from perplexity_webui_scraper import MODELS as _MODELS
    from perplexity_webui_scraper import CitationMode as _CitationMode
    from perplexity_webui_scraper import Coordinates as _Coordinates
    
    Perplexity = _Perplexity
    ConversationConfig = _ConversationConfig
    MODELS = _MODELS
    CitationMode = _CitationMode
    Coordinates = _Coordinates
    
    # Tenta importar TimeRange e SourceFocus
    try:
        from perplexity_webui_scraper import SourceFocus as _SourceFocus
        SourceFocus = _SourceFocus
    except ImportError:
        logger.warning("⚠️ SourceFocus não disponível")
        SourceFocus = None

    try:
        from perplexity_webui_scraper import TimeRange as _TimeRange
        TimeRange = _TimeRange
    except ImportError:
        logger.warning("⚠️ TimeRange não disponível")
        TimeRange = None
    
    SCRAPER_AVAILABLE = True
    logger.info("✅ Scraper perplexity-webui-scraper carregado com sucesso!")
    
    # Log dos modelos disponíveis
    if MODELS:
        available_models = [model.id for model in MODELS.list_all()]
        logger.info(f"📋 Modelos disponíveis: {available_models[:5]}...")
        
except ImportError as e:
    logger.warning(f"⚠️ Scraper não instalado: {e}")
    logger.warning("Instale com: pip install git+https://github.com/henrique-coder/perplexity-webui-scraper")

# ============= CLIENTE PERPLEXITY =============

# ============= GERENCIADOR DE CLIENTES =============

def apply_extra_cookies(client, extra_cookies: dict) -> None:
    """Aplica cookies complementares no HTTP session do scraper novo."""
    if not client or not extra_cookies:
        return

    try:
        session = client._http._session
        session.cookies.update(extra_cookies)
    except Exception as e:
        logger.warning(f"Aviso ao aplicar cookies complementares: {e}")


class ClientManager:
    def __init__(self):
        self.default_client: Optional[Perplexity] = None
        self.location_clients: Dict[str, Perplexity] = {} # "lat,lon" -> client
        self.session_token = ""
        
    def init_default(self, token: str, extra_cookies: dict = None):
        self.default_client = None
        self.location_clients = {}
        self.session_token = token or ""

        if not token:
            logger.info("🧹 Cliente Default limpo")
            return

        if SCRAPER_AVAILABLE and Perplexity and token != "seu_session_token_aqui":
            try:
                # Lê cookies complementares (cf_clearance, etc.)
                if extra_cookies is None and token_manager:
                    extra_cookies = token_manager.get_complementary_cookies()
                
                self.default_client = Perplexity(session_token=token)
                apply_extra_cookies(self.default_client, extra_cookies or {})
                
                if extra_cookies:
                    logger.info(f"✅ Cliente Default inicializado com {len(extra_cookies)} cookies complementares!")
                else:
                    logger.info("✅ Cliente Default inicializado!")
            except Exception as e:
                logger.error(f"❌ Erro config default client: {e}")

    def get_client(self, lat: float = None, lon: float = None) -> Optional[Perplexity]:
        # Se não tem coords, usa default
        if lat is None or lon is None:
            return self.default_client
        
        # Sempre usa o cliente default (localização fixa: Brasil)
        return self.default_client

client_manager = ClientManager()

# Inicializa cliente usando TokenManager (prioridade) ou fallback .env
if token_manager and token_manager.accounts:
    # Usa token do TokenManager
    current_token = token_manager.get_current_token()
    if current_token:
        client_manager.init_default(current_token)
        account_info = token_manager.get_account_info()
        logger.info(f"🔑 Usando token do TokenManager: {account_info.get('name', 'unknown')}")
elif PERPLEXITY_SESSION_TOKEN:
    # Fallback para variável de ambiente
    client_manager.init_default(PERPLEXITY_SESSION_TOKEN)
    logger.info("📌 Usando PERPLEXITY_SESSION_TOKEN do .env")

# ============= STORAGE DE CONVERSAS ATIVAS =============
# Mantém uma conversa ativa por usuário para histórico nativo
active_conversations: Dict[str, Any] = {}
conversation_message_counts: Dict[str, int] = {}
conversation_messages: Dict[str, List[Dict[str, str]]] = {}  # Armazena mensagens para salvar
conversation_last_activity: Dict[str, float] = {}  # Timestamp da última atividade por user
SAVE_TO_LIBRARY_ENABLED = False  # Default: False (Evita erro 403 na VPN)


def get_active_client():
    """Retorna o cliente ativo do client_manager (sempre atualizado após rotação)"""
    return client_manager.default_client


def _get_auth_retry_limit() -> int:
    """Uma busca sempre ganha ao menos uma tentativa de refresh automático."""
    if not token_manager:
        return 1
    return 3 if len(token_manager.accounts) > 1 else 2


def _is_auth_error(err_str: str) -> bool:
    err_str = (err_str or "").lower()
    return any(term in err_str for term in ("401", "403", "unauthorized", "forbidden"))


def _classify_auth_failure(err_str: str) -> str:
    err_str = (err_str or "").lower()
    if any(term in err_str for term in ("cloudflare", "cf_clearance", "cf blocked", "cf_blocked")):
        return "cf_blocked"
    return "session_expired"


def _recover_auth_failure(user_id: str, err_str: str, attempt: int, max_retries: int, route_name: str) -> Optional[str]:
    """
    Tenta recuperar automaticamente falhas de autenticação do Perplexity.
    Ordem: refresh da sessão atual e, se não resolver, rotação para outra conta.
    """
    if not token_manager or attempt >= max_retries - 1:
        return None

    current_token = client_manager.session_token or None
    current_token_id = token_manager.get_token_id(current_token) if current_token else None
    failure_reason = _classify_auth_failure(err_str)

    if current_token:
        if failure_reason == "cf_blocked":
            token_manager.mark_cf_blocked(token=current_token)
        else:
            token_manager.mark_invalid(token=current_token, reason=failure_reason)

    refresh_result = None
    try:
        if current_token_id:
            refresh_result = token_manager.refresh_token(token_id=current_token_id)
        else:
            refresh_result = token_manager.refresh_token()
    except Exception as refresh_err:
        logger.warning(f"⚠️ Refresh automático falhou em {route_name}: {refresh_err}")

    if refresh_result and refresh_result.get("success"):
        refreshed_token = refresh_result.get("new_token") or current_token or token_manager.get_current_token()
        if refreshed_token:
            client_manager.init_default(refreshed_token)
            active_conversations.pop(user_id, None)
            logger.warning(f"🔄 Auth falhou em {route_name}; sessão renovada automaticamente")
            return "refresh"

    next_token, _ = token_manager.get_next_valid_token()
    if next_token and next_token != current_token:
        client_manager.init_default(next_token)
        active_conversations.pop(user_id, None)
        logger.warning(f"🔄 Auth falhou em {route_name}; alternando para outra credencial")
        return "rotate"

    return None


def _runtime_credentials_status() -> Dict[str, Any]:
    """Status sanitizado para a UI de credenciais."""
    pool_status = token_manager.get_pool_status() if token_manager else {}
    return {
        "configured": bool(token_manager and token_manager.get_current_token()),
        "client_ready": get_active_client() is not None,
        "token_count": pool_status.get("total", 0),
        "has_complementary_cookies": pool_status.get("has_complementary_cookies", False),
        "cookies_status": pool_status.get("cookies_status", "unknown"),
        "cookies_updated_at": pool_status.get("cookies_updated_at"),
        "api_key_required": bool(MCP_API_KEY),
        "env_fallback_present": bool(os.getenv("PERPLEXITY_SESSION_TOKEN")),
    }


def _credentials_ui_auth_error():
    """Autenticação leve para a UI de credenciais."""
    if not MCP_API_KEY:
        return None

    provided_key = request.headers.get("X-API-Key", "")
    if provided_key != MCP_API_KEY:
        return jsonify({
            "success": False,
            "message": "Informe a chave administrativa para usar esta página."
        }), 401

    return None


def cleanup_expired_conversations():
    """
    Remove conversas que estão inativas há mais de CONVERSATION_TTL_SECONDS.
    Chamado periodicamente por uma thread de background.
    """
    now = time.time()
    expired = [
        uid for uid, ts in conversation_last_activity.items()
        if now - ts > CONVERSATION_TTL_SECONDS
    ]
    
    for uid in expired:
        # Salva conversa antes de limpar
        if uid in conversation_messages and conversation_messages[uid]:
            try:
                save_conversation(uid)
            except Exception as e:
                logger.warning(f"Erro ao salvar conversa expirada {uid}: {e}")
        
        active_conversations.pop(uid, None)
        conversation_message_counts.pop(uid, None)
        conversation_messages.pop(uid, None)
        conversation_last_activity.pop(uid, None)
    
    if expired:
        logger.info(f"🧹 Cleanup: {len(expired)} conversa(s) expirada(s) removida(s)")
    
    # Limite absoluto: se exceder MAX_ACTIVE_CONVERSATIONS, remove as mais antigas
    if len(active_conversations) > MAX_ACTIVE_CONVERSATIONS:
        sorted_users = sorted(conversation_last_activity.items(), key=lambda x: x[1])
        excess = len(active_conversations) - MAX_ACTIVE_CONVERSATIONS
        for uid, _ in sorted_users[:excess]:
            if uid in conversation_messages and conversation_messages[uid]:
                try:
                    save_conversation(uid)
                except Exception:
                    pass
            active_conversations.pop(uid, None)
            conversation_message_counts.pop(uid, None)
            conversation_messages.pop(uid, None)
            conversation_last_activity.pop(uid, None)
        logger.info(f"🧹 Cleanup: {excess} conversa(s) removida(s) por limite")


def _cleanup_thread():
    """Thread de cleanup periódico (a cada 5 minutos)"""
    while True:
        time.sleep(300)  # 5 minutos
        try:
            cleanup_expired_conversations()
        except Exception as e:
            logger.error(f"Erro no cleanup thread: {e}")


# Inicia thread de cleanup
_cleanup = threading.Thread(target=_cleanup_thread, daemon=True)
_cleanup.start()
logger.info(f"🧹 Cleanup thread iniciada (TTL={CONVERSATION_TTL_SECONDS}s, Max={MAX_ACTIVE_CONVERSATIONS})")

# Diretório para salvar conversas
CONVERSATIONS_DIR = Path(os.getenv("CONVERSATIONS_DIR", "./data/conversations"))
CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"📁 Diretório de conversas: {CONVERSATIONS_DIR.absolute()}")


def save_conversation(user_id: str) -> Optional[str]:
    """
    Salva a conversa atual do usuário em um arquivo JSON.
    Retorna o ID da conversa salva ou None se não houver conversa.
    """
    if user_id not in conversation_messages or not conversation_messages[user_id]:
        return None
    
    conv_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().isoformat()
    
    # Gera título a partir da primeira mensagem do usuário
    first_msg = ""
    for msg in conversation_messages[user_id]:
        if msg.get('role') == 'user':
            first_msg = msg.get('content', '')[:50]
            break
    
    title = first_msg + "..." if len(first_msg) >= 50 else first_msg
    if not title:
        title = f"Conversa {conv_id}"
    
    # Cria objeto da conversa
    conversation_data = {
        "id": conv_id,
        "user_id": user_id,
        "title": title,
        "created_at": timestamp,
        "message_count": len(conversation_messages[user_id]),
        "messages": conversation_messages[user_id]
    }
    
    # Cria pasta do usuário
    user_dir = CONVERSATIONS_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    
    # Salva arquivo
    file_path = user_dir / f"{conv_id}.json"
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(conversation_data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"[💾 SAVE] Conversa {conv_id} salva para user_id={user_id} ({len(conversation_messages[user_id])} msgs)")
    return conv_id


def reset_runtime_conversations(save_existing: bool = True) -> Dict[str, int]:
    """
    Limpa todas as conversas ativas da memória.
    Opcionalmente persiste o histórico antes de limpar.
    """
    saved = 0
    cleared = len(active_conversations)

    for user_id in list(conversation_messages.keys()):
        if save_existing and conversation_messages.get(user_id):
            try:
                if save_conversation(user_id):
                    saved += 1
            except Exception as e:
                logger.warning(f"Erro ao salvar conversa antes do reset ({user_id}): {e}")

    active_conversations.clear()
    conversation_message_counts.clear()
    conversation_messages.clear()
    conversation_last_activity.clear()

    logger.info(f"🧹 Runtime limpo: {cleared} conversa(s) removida(s), {saved} salva(s)")
    return {"saved": saved, "cleared": cleared}


def list_saved_conversations(user_id: str) -> List[Dict[str, Any]]:
    """
    Lista todas as conversas salvas de um usuário.
    """
    user_dir = CONVERSATIONS_DIR / user_id
    if not user_dir.exists():
        return []
    
    conversations = []
    for file_path in sorted(user_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                conversations.append({
                    "id": data.get("id"),
                    "title": data.get("title"),
                    "created_at": data.get("created_at"),
                    "message_count": data.get("message_count", 0)
                })
        except Exception as e:
            logger.warning(f"Erro ao ler {file_path}: {e}")
    
    return conversations[:20]  # Limita a 20 conversas


def load_conversation(user_id: str, conv_id: str) -> Optional[Dict[str, Any]]:
    """
    Carrega uma conversa salva pelo ID.
    """
    file_path = CONVERSATIONS_DIR / user_id / f"{conv_id}.json"
    if not file_path.exists():
        return None
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Erro ao carregar conversa {conv_id}: {e}")
        return None


def delete_saved_conversation(user_id: str, conv_id: str) -> bool:
    """
    Deleta uma conversa salva.
    """
    file_path = CONVERSATIONS_DIR / user_id / f"{conv_id}.json"
    if file_path.exists():
        file_path.unlink()
        logger.info(f"[🗑️ DELETE] Conversa {conv_id} deletada")
        return True
    return False


def get_model_enum(model_id: str):
    """Converte ID do modelo para enum do scraper"""
    if not SCRAPER_AVAILABLE or Models is None:
        return None
    
    model_id = model_id.lower().replace("-", "_").replace(".", "_")
    
    # Mapeamento de IDs amigáveis para atributos do enum
    id_to_attr = {
        "best": "BEST",
        "sonar": "SONAR",
        "deep_research": "DEEP_RESEARCH",
        # GPT-5.4 (antigo GPT-5.2)
        "gpt_5_4": "GPT_54",
        "gpt_5_4_thinking": "GPT_54_THINKING",
        # Compat: aliases antigos do GPT-5.2 → GPT-5.4
        "gpt_5_2": "GPT_54",
        "gpt_5_2_thinking": "GPT_54_THINKING",
        # Claude Sonnet 4.6
        "claude_sonnet_4_6": "CLAUDE_46_SONNET",
        "claude_sonnet_4_6_thinking": "CLAUDE_46_SONNET_THINKING",
        "claude_4_6_sonnet": "CLAUDE_46_SONNET",
        "claude_4_6_sonnet_thinking": "CLAUDE_46_SONNET_THINKING",
        # Claude Opus 4.6
        "claude_opus_4_6": "CLAUDE_46_OPUS",
        "claude_opus_4_6_thinking": "CLAUDE_46_OPUS_THINKING",
        "claude_4_6_opus": "CLAUDE_46_OPUS",
        "claude_4_6_opus_thinking": "CLAUDE_46_OPUS_THINKING",
        # Gemini 3 Flash
        "gemini_3_flash": "GEMINI_3_FLASH",
        "gemini_3_flash_thinking": "GEMINI_3_FLASH_THINKING",
        # Gemini 3.1 Pro (novo)
        "gemini_3_1_pro": "GEMINI_31_PRO",
        "gemini_3_1_pro_thinking": "GEMINI_31_PRO_THINKING",
        "gemini_3_pro_thinking": "GEMINI_3_PRO_THINKING",
        # Grok 4.1
        "grok_4_1": "GROK_41",
        "grok_4_1_thinking": "GROK_41_THINKING",
        # Kimi K2.5
        "kimi_k2_5_thinking": "KIMI_K25_THINKING",
        # NVIDIA Nemotron 3 Super (novo)
        "nv_nemotron_3_super_thinking": "NEMOTRON_3_SUPER",
        "nemotron": "NEMOTRON_3_SUPER",
        # Compat legado
        "create_files": "CREATE_FILES_AND_APPS",
    }
    
    attr_name = id_to_attr.get(model_id, "BEST")
    
    # Tenta obter o atributo do enum
    if hasattr(Models, attr_name):
        return getattr(Models, attr_name)
    
    # Fallback para BEST
    return getattr(Models, "BEST", None)


def get_source_focus(focus_id: str):
    """Converte ID do focus para enum do scraper (se disponível)"""
    if not SCRAPER_AVAILABLE or SourceFocus is None:
        return None
    
    focus_id = focus_id.upper()
    
    # Tenta obter o atributo
    if hasattr(SourceFocus, focus_id):
        return getattr(SourceFocus, focus_id)
    
    # Fallback para WEB
    return getattr(SourceFocus, "WEB", None)


def get_citation_mode(mode: str):
    """Converte modo de citação para enum"""
    if not SCRAPER_AVAILABLE or CitationMode is None:
        return None
    
    mode = mode.upper()
    
    if hasattr(CitationMode, mode):
        return getattr(CitationMode, mode)
    
    return getattr(CitationMode, "MARKDOWN", None)


def get_time_range(range_id: str):
    """Converte ID de tempo para enum"""
    if not SCRAPER_AVAILABLE or TimeRange is None:
        return None
        
    range_id = range_id.upper()
    mapping = {
        "ALL": "ALL",
        "DAY": "TODAY",
        "WEEK": "LAST_WEEK", 
        "MONTH": "LAST_MONTH",
        "YEAR": "LAST_YEAR"
    }
    
    attr = mapping.get(range_id, "ALL")
    return getattr(TimeRange, attr, TimeRange.ALL)


MODEL_ALIASES = {
    "best": "perplexity/best",
    "sonar": "perplexity/sonar-2",
    "sonar-2": "perplexity/sonar-2",
    "deep-research": "perplexity/deep-research",
    "deep_research": "perplexity/deep-research",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt_5_4": "openai/gpt-5.4",
    "gpt-5.4-thinking": "openai/gpt-5.4-thinking",
    "gpt_5_4_thinking": "openai/gpt-5.4-thinking",
    "gpt-5.2": "openai/gpt-5.4",
    "gpt-5.2-thinking": "openai/gpt-5.4-thinking",
    "claude-sonnet-4.6": "anthropic/claude-sonnet-4.6",
    "claude-sonnet-4.6-thinking": "anthropic/claude-sonnet-4.6-thinking",
    "claude-4.6-sonnet": "anthropic/claude-sonnet-4.6",
    "claude-4.6-sonnet-thinking": "anthropic/claude-sonnet-4.6-thinking",
    "claude-opus-4.7": "anthropic/claude-opus-4.7",
    "claude-opus-4.7-thinking": "anthropic/claude-opus-4.7-thinking",
    "claude-opus-4.6": "anthropic/claude-opus-4.7",
    "claude-opus-4.6-thinking": "anthropic/claude-opus-4.7-thinking",
    "claude-4.6-opus": "anthropic/claude-opus-4.7",
    "claude-4.6-opus-thinking": "anthropic/claude-opus-4.7-thinking",
    "gemini-3.1-pro-thinking-low": "google/gemini-3.1-pro-thinking-low",
    "gemini-3.1-pro-thinking-high": "google/gemini-3.1-pro-thinking-high",
    "gemini-3.1-pro": "google/gemini-3.1-pro-thinking-low",
    "gemini-3-flash": "google/gemini-3.1-pro-thinking-low",
    "gemini-3-flash-thinking": "google/gemini-3.1-pro-thinking-high",
    "gemini-3-pro-thinking": "google/gemini-3.1-pro-thinking-high",
    "kimi-k2.6-instant": "moonshot/kimi-k2.6-instant",
    "kimi-k2.6-thinking": "moonshot/kimi-k2.6-thinking",
    "kimi-k2.5-thinking": "moonshot/kimi-k2.6-thinking",
    "nv-nemotron-3-super-thinking": "nvidia/nemotron-3-super-thinking",
    "nemotron": "nvidia/nemotron-3-super-thinking",
}

MODEL_ALIAS_GROUPS = {}
for _alias, _canonical in MODEL_ALIASES.items():
    MODEL_ALIAS_GROUPS.setdefault(_canonical, []).append(_alias)


def resolve_model_id(model_id: str) -> str:
    """Converte aliases antigos para ids canonicos do scraper novo."""
    raw = (model_id or "best").strip()
    normalized = raw.lower().replace("_", "-")
    candidate = MODEL_ALIASES.get(normalized, raw)

    if MODELS is None:
        return candidate

    try:
        MODELS.resolve(candidate)
        return candidate
    except Exception:
        logger.warning(f"Modelo desconhecido '{model_id}', usando perplexity/best")
        return "perplexity/best"


def get_model_enum(model_id: str):
    """Compatibilidade: agora retorna string canonica, nao enum."""
    return resolve_model_id(model_id)


def get_source_focus(focus_id: str):
    focus_id = (focus_id or "web").lower().replace("_", "-")
    if focus_id in {"web", "academic", "social", "finance", "all"}:
        return focus_id
    return "web"


def get_search_focus(focus_id: str):
    focus_id = (focus_id or "web").lower().replace("_", "-")
    return "writing" if focus_id == "writing" else "web"


def get_citation_mode(mode: str):
    mode = (mode or "markdown").lower().replace("_", "-")
    if mode in {"default", "markdown", "clean"}:
        return mode
    return "markdown"


def get_time_range(range_id: str):
    range_id = (range_id or "all").lower().replace("_", "-")
    if range_id in {"all", "day", "week", "month", "year"}:
        return range_id
    return "all"


def get_conversation_uuid(conversation) -> Optional[str]:
    return getattr(conversation, "uuid", None) or getattr(conversation, "backend_uuid", None)


def get_raw_data(obj) -> dict:
    raw = getattr(obj, "raw_data", None)
    if raw is None:
        raw = getattr(obj, "_raw_data", None)
    return raw if isinstance(raw, dict) else {}


# ============= ENDPOINTS =============

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint com verificação de conectividade"""
    import socket
    
    # Verifica conectividade com Perplexity
    perplexity_reachable = False
    try:
        sock = socket.create_connection(("www.perplexity.ai", 443), timeout=5)
        sock.close()
        perplexity_reachable = True
    except Exception as e:
        logger.debug(f"Health check: Perplexity unreachable - {e}")
    
    # Determina status geral
    checks = {
        "scraper": SCRAPER_AVAILABLE,
        "client": get_active_client() is not None,
        "token_manager": token_manager is not None and len(token_manager.accounts) > 0,
        "perplexity_connectivity": perplexity_reachable
    }
    
    # Status: ok se scraper + client + connectivity, degraded caso contrário
    if checks["scraper"] and checks["client"] and checks["perplexity_connectivity"]:
        status = "healthy"
    elif checks["scraper"] and checks["client"]:
        status = "degraded"  # Funciona mas pode ter problemas de rede
    else:
        status = "unhealthy"
    
    return jsonify({
        "status": status,
        "checks": checks,
        "scraper_available": SCRAPER_AVAILABLE,
        "client_initialized": get_active_client() is not None,
        "source_focus_available": SourceFocus is not None,
        "token_manager_active": token_manager is not None and len(token_manager.accounts) > 0,
        "perplexity_reachable": perplexity_reachable,
        "active_conversations": len(active_conversations),
        "max_conversations": MAX_ACTIVE_CONVERSATIONS,
        "conversation_ttl": CONVERSATION_TTL_SECONDS,
        "api_key_required": bool(MCP_API_KEY),
        "version": "3.1.0"
    })


@app.route('/credentials', methods=['GET'])
def credentials_page():
    """Página simples para cadastrar, testar e apagar credenciais sem exibir segredos."""
    return render_template("credentials.html")


@app.route('/credentials/api/status', methods=['GET'])
def credentials_status():
    auth_error = _credentials_ui_auth_error()
    if auth_error:
        return auth_error

    return jsonify({
        "success": True,
        "status": _runtime_credentials_status(),
    })


@app.route('/credentials/api/save', methods=['POST'])
@limiter.limit("10 per minute")
def credentials_save():
    auth_error = _credentials_ui_auth_error()
    if auth_error:
        return auth_error

    if not token_manager:
        return jsonify({"success": False, "message": "TokenManager não disponível"}), 503

    data = request.json or {}
    raw = str(data.get("input", "")).strip()
    name = str(data.get("name", "")).strip() or None

    if not raw:
        return jsonify({"success": False, "message": "Cole um token, cookie string ou JSON de cookies."}), 400

    result = token_manager.set_token(
        raw,
        name=name,
        source="credentials_ui",
        validate=False,
    )

    if not result.get("success"):
        return jsonify({
            "success": False,
            "message": result.get("message", "Falha ao salvar credencial."),
        }), 400

    current = token_manager.get_current_token()
    if current:
        client_manager.init_default(current)

    runtime = reset_runtime_conversations(save_existing=True)
    return jsonify({
        "success": True,
        "message": "Credencial salva. Clique em testar para validar.",
        "runtime": runtime,
        "status": _runtime_credentials_status(),
    })


@app.route('/credentials/api/test', methods=['POST'])
@limiter.limit("10 per minute")
def credentials_test():
    auth_error = _credentials_ui_auth_error()
    if auth_error:
        return auth_error

    if not token_manager:
        return jsonify({"success": False, "message": "TokenManager não disponível"}), 503

    current = token_manager.get_current_token()
    if not current:
        client_manager.init_default(None)
        return jsonify({
            "success": False,
            "message": "Nenhuma credencial cadastrada para testar.",
            "status": _runtime_credentials_status(),
        }), 400

    is_valid = token_manager.validate_token(current)
    if is_valid:
        client_manager.init_default(current)
        message = "Credencial válida e pronta para uso."
        status_code = 200
    else:
        client_manager.init_default(None)
        message = "Credencial inválida, expirada ou bloqueada."
        status_code = 400

    return jsonify({
        "success": is_valid,
        "message": message,
        "status": _runtime_credentials_status(),
    }), status_code


@app.route('/credentials/api/clear', methods=['POST'])
@limiter.limit("10 per minute")
def credentials_clear():
    auth_error = _credentials_ui_auth_error()
    if auth_error:
        return auth_error

    runtime = reset_runtime_conversations(save_existing=True)

    details = {}
    if token_manager:
        details = token_manager.clear_all_tokens()
        token_manager._env_token = ""

    global PERPLEXITY_SESSION_TOKEN
    PERPLEXITY_SESSION_TOKEN = ""
    os.environ["PERPLEXITY_SESSION_TOKEN"] = ""
    client_manager.init_default(None)

    return jsonify({
        "success": True,
        "message": "Credenciais apagadas do armazenamento local e do runtime atual.",
        "runtime": runtime,
        "details": details,
        "status": _runtime_credentials_status(),
    })


@app.route('/tokens', methods=['GET'])
@app.route('/tokens/status', methods=['GET'])
@require_api_key
def tokens_status():
    """Status do pool + conta ativa (unificado v3)"""
    if token_manager is None:
        return jsonify({
            "active": False,
            "message": "TokenManager n\u00e3o dispon\u00edvel",
            "total": 0, "valid": 0, "cf_blocked": 0, "invalid": 0, "unknown": 0
        })
    
    status = token_manager.get_pool_status()
    status["active"] = status.get("active", False)
    status["total_accounts"] = status["total"]
    status["current_index"] = 0
    status["rotation_enabled"] = True
    status["tokens_dir"] = str(token_manager.tokens_dir)
    return jsonify(status)


@app.route('/tokens/set', methods=['POST'])
@require_api_key
@limiter.limit("10 per minute")
def tokens_set():
    """
    Unificado v3: recebe JWT, JSON array, ou cookie string.
    Detecta formato automaticamente.
    Body: {"input": "..."} ou {"token": "..."} ou {"cookie_string": "..."} ou {"cookies": [...]}
    """
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    
    data = request.json or {}
    raw = data.get("input") or data.get("token") or data.get("cookie_string") or ""
    if not raw and data.get("cookies"):
        raw = json.dumps(data["cookies"])
    if not raw:
        return jsonify({"success": False, "message": "Campo 'input' obrigat\u00f3rio"}), 400
    
    name = data.get("name")
    validate = data.get("validate", True)
    result = token_manager.set_token(str(raw), name=name, validate=validate)
    
    if result.get("success") and not result.get("duplicate"):
        current = token_manager.get_current_token()
        if current:
            client_manager.init_default(current)
        reset_runtime_conversations(save_existing=True)
    
    result["token_preview"] = f"****{result.get('token_id', '????')[-4:]}" if result.get("token_id") else "????"
    result["cookies_count"] = len(token_manager.get_complementary_cookies())
    result["rotated"] = not result.get("duplicate", False)
    result["refresh_message"] = result.get("message", "")
    pool_status = token_manager.get_pool_status()
    result["pool_total"] = pool_status.get("total", 0)
    result["pool_valid"] = pool_status.get("valid", 0)
    result["has_session_token"] = result.get("success", False)
    
    return jsonify(result), 200 if result["success"] else 400


@app.route('/tokens/validate', methods=['POST', 'GET'])
@require_api_key
def tokens_validate():
    """Valida o token atual com probe real"""
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 400
    
    is_valid = token_manager.validate_token()
    pool_status = token_manager.get_pool_status()
    return jsonify({
        "valid": is_valid,
        "account": pool_status["current_account"],
        "pool_status": pool_status,
        "message": "Token v\u00e1lido" if is_valid else "Token inv\u00e1lido ou bloqueado",
        "token_preview": f"****{token_manager.get_current_token()[-8:]}" if token_manager.get_current_token() else None
    })


@app.route('/tokens/rotate', methods=['POST'])
@require_api_key
def tokens_rotate():
    """Rotaciona para o pr\u00f3ximo token v\u00e1lido"""
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    
    result = token_manager.rotate()
    if result.get("success") and result.get("token"):
        client_manager.init_default(result["token"])
    
    result["new_index"] = 0
    result["account"] = result.get("account", {})
    return jsonify(result)


@app.route('/tokens/pool', methods=['GET', 'POST'])
@require_api_key
def tokens_pool():
    """
    GET=status, POST=actions: validate_all, clear_invalid, smart_refresh, refresh, clear_all
    """
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    
    if request.method == "GET":
        return jsonify(token_manager.get_pool_status())
    
    data = request.json or {}
    action = data.get("action", "")
    
    if action == "validate_all":
        results = token_manager.validate_all_pool()
        return jsonify({"action": "validate_all", "results": results})
    elif action == "clear_invalid":
        removed = token_manager.clear_invalid()
        return jsonify({"action": "clear_invalid", "removed": removed})
    elif action == "smart_refresh":
        results = token_manager.smart_refresh_all()
        current = token_manager.get_current_token()
        if current:
            client_manager.init_default(current)
        return jsonify({"action": "smart_refresh", "results": results})
    elif action == "refresh":
        result = token_manager.refresh_token()
        if result.get("success") and result.get("new_token"):
            client_manager.init_default(result["new_token"])
        return jsonify({"status": "success" if result["success"] else "error", "message": result["message"]})
    elif action == "clear_all":
        runtime = reset_runtime_conversations(save_existing=True)
        results = token_manager.clear_all_tokens()
        token_manager._env_token = ""
        global PERPLEXITY_SESSION_TOKEN
        PERPLEXITY_SESSION_TOKEN = ""
        os.environ["PERPLEXITY_SESSION_TOKEN"] = ""
        client_manager.init_default(None)
        return jsonify({"status": "success", "message": "Todos os tokens apagados", "details": results, "runtime": runtime})
    
    return jsonify({"error": f"A\u00e7\u00e3o desconhecida: {action}"}), 400


# === COMPAT: endpoints legados ===

@app.route('/tokens/refresh', methods=['POST', 'GET'])
@require_api_key
@limiter.limit("5 per minute")
def tokens_refresh_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    result = token_manager.refresh_token()
    if result.get("success") and result.get("new_token"):
        client_manager.init_default(result["new_token"])
    return jsonify({"status": "success" if result["success"] else "error", "message": result["message"]})

@app.route('/tokens/upload_cookies', methods=['POST'])
@require_api_key
@limiter.limit("10 per minute")
def tokens_upload_cookies_compat():
    data = request.json or {}
    raw = data.get("cookie_string") or ""
    if not raw and data.get("cookies"):
        raw = json.dumps(data["cookies"])
    if not raw:
        return jsonify({"error": "Envie 'cookies' ou 'cookie_string'"}), 400
    result = token_manager.set_token(raw, validate=True)
    if result.get("success"):
        current = token_manager.get_current_token()
        if current:
            client_manager.init_default(current)
    return jsonify(result)

@app.route('/tokens/reload', methods=['POST'])
@require_api_key
def tokens_reload_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    token_manager.reload_tokens()
    current = token_manager.get_current_token()
    if current:
        client_manager.init_default(current)
    return jsonify({"status": "ok", "details": token_manager.get_status()})

@app.route('/tokens/pool/status', methods=['GET'])
@require_api_key
def tokens_pool_status_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    return jsonify(token_manager.get_pool_status())

@app.route('/tokens/pool/smart_refresh', methods=['POST'])
@require_api_key
def tokens_pool_smart_refresh_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    results = token_manager.smart_refresh_all()
    current = token_manager.get_current_token()
    if current:
        client_manager.init_default(current)
    return jsonify(results)

@app.route('/tokens/pool/validate_all', methods=['POST'])
@require_api_key
def tokens_pool_validate_all_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    return jsonify(token_manager.validate_all_pool())

@app.route('/tokens/pool/clear_invalid', methods=['POST'])
@require_api_key
def tokens_pool_clear_invalid_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    removed = token_manager.clear_invalid()
    return jsonify({"removed": removed})

@app.route('/tokens/clear_all', methods=['POST'])
@require_api_key
def tokens_clear_all_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\u00e3o dispon\u00edvel"}), 503
    runtime = reset_runtime_conversations(save_existing=True)
    results = token_manager.clear_all_tokens()
    token_manager._env_token = ""
    global PERPLEXITY_SESSION_TOKEN
    PERPLEXITY_SESSION_TOKEN = ""
    os.environ["PERPLEXITY_SESSION_TOKEN"] = ""
    client_manager.init_default(None)
    return jsonify({"status": "success", "details": results, "runtime": runtime})


# ============= CANVAS / FILE HELPERS =============

# Mapeamento de linguagem para extensão
_CANVAS_EXT_MAP = {
    'html': '.html', 'htm': '.html',
    'css': '.css',
    'js': '.js', 'javascript': '.js', 'typescript': '.ts', 'ts': '.ts',
    'jsx': '.jsx', 'tsx': '.tsx',
    'py': '.py', 'python': '.py',
    'java': '.java',
    'c': '.c', 'cpp': '.cpp', 'c++': '.cpp',
    'cs': '.cs', 'csharp': '.cs',
    'php': '.php',
    'sql': '.sql',
    'json': '.json',
    'xml': '.xml',
    'yaml': '.yaml', 'yml': '.yaml',
    'md': '.md', 'markdown': '.md',
    'sh': '.sh', 'bash': '.sh', 'shell': '.sh',
    'txt': '.txt',
    'svg': '.svg',
    'csv': '.csv',
    'dockerfile': '',
    'react': '.jsx',
    'vue': '.vue',
    'svelte': '.svelte',
}


def _canvas_filename(title: str, lang: str) -> str:
    """Gera nome de arquivo inteligente a partir do título e linguagem."""
    lang = (lang or 'txt').lower().strip()
    ext = _CANVAS_EXT_MAP.get(lang, '.txt')
    
    if title:
        # Limpa título para usar como nome de arquivo
        import re
        safe_title = re.sub(r'[^\w\s\-.]', '', title).strip()
        safe_title = re.sub(r'\s+', '_', safe_title)[:50]
        if safe_title:
            # Se o título já tem extensão, usa ele diretamente
            if any(safe_title.lower().endswith(e) for e in _CANVAS_EXT_MAP.values() if e):
                return safe_title
            return f"{safe_title}{ext}"
    
    return f"canvas_file{ext}"


def _extract_canvas_files(canvas_data) -> list:
    """
    Extrai arquivos do objeto canvas/blocks do Perplexity.
    Tenta múltiplos formatos possíveis.
    """
    files = []
    
    def _extract_from_dict(d, index=0):
        """Extrai arquivo de um dict de canvas."""
        content = (
            d.get('code') or d.get('content') or d.get('text') or
            d.get('html') or d.get('source') or d.get('body') or ''
        )
        lang = (
            d.get('language') or d.get('type') or d.get('lang') or
            d.get('file_type') or d.get('extension') or 'txt'
        )
        title = (
            d.get('title') or d.get('name') or d.get('filename') or
            d.get('file_name') or ''
        )
        
        # Detecta HTML pelo conteúdo se a linguagem não está definida
        if isinstance(content, str) and content.strip():
            content_lower = content.strip()[:50].lower()
            if content_lower.startswith('<!doctype') or content_lower.startswith('<html'):
                lang = 'html'
        
        if isinstance(content, str) and len(content.strip()) > 20:
            files.append({
                'filename': _canvas_filename(title or f'file_{index+1}', lang),
                'content': content,
                'language': lang
            })
    
    if isinstance(canvas_data, dict):
        # Pode ser um único arquivo
        has_content = any(k in canvas_data for k in ['code', 'content', 'text', 'html', 'source', 'body'])
        if has_content:
            _extract_from_dict(canvas_data)
        else:
            # Pode ser um dict de múltiplos arquivos: {"file1": {...}, "file2": {...}}
            for i, (key, val) in enumerate(canvas_data.items()):
                if isinstance(val, dict):
                    _extract_from_dict(val, i)
                elif isinstance(val, str) and len(val.strip()) > 20:
                    files.append({
                        'filename': _canvas_filename(key, 'txt'),
                        'content': val,
                        'language': 'txt'
                    })
    
    elif isinstance(canvas_data, list):
        for i, item in enumerate(canvas_data):
            if isinstance(item, dict):
                _extract_from_dict(item, i)
            elif isinstance(item, str) and len(item.strip()) > 20:
                files.append({
                    'filename': f'file_{i+1}.txt',
                    'content': item,
                    'language': 'txt'
                })
    
    elif isinstance(canvas_data, str) and len(canvas_data.strip()) > 20:
        # Canvas é string direta (ex: HTML bruto)
        lang = 'html' if canvas_data.strip()[:20].lower().startswith(('<html', '<!doc')) else 'txt'
        files.append({
            'filename': _canvas_filename('', lang),
            'content': canvas_data,
            'language': lang
        })
    
    return files


@app.route('/search_stream', methods=['POST'])
@require_api_key
@limiter.limit("20 per minute")
def search_stream():
    """
    Endpoint de busca com STREAMING (SSE).
    Retorna eventos: status, thinking, citation, chunk, done.
    """
    try:
        data = request.json or {}
        query = data.get('query')
        user_id = str(data.get('user_id', 'default'))
        model_id = data.get('model', 'best')
        focus_id = data.get('focus', 'web')
        time_range_id = data.get('time_range', 'all')
        
        if not query:
            return jsonify({"error": "Query required"}), 400

        active_client = get_active_client()
        if not SCRAPER_AVAILABLE or active_client is None:
             return jsonify({"error": "Service unavailable"}), 503

        # Atualiza timestamp de atividade
        conversation_last_activity[user_id] = time.time()

        # Configuração
        model_enum = get_model_enum(model_id)
        config_kwargs = {
            "model": model_enum,
            "language": "pt-BR",
            "save_to_library": SAVE_TO_LIBRARY_ENABLED,
            "search_focus": get_search_focus(focus_id),
        }
        
        if SourceFocus is not None:
             source_focus_enum = get_source_focus(focus_id)
             if source_focus_enum:
                 config_kwargs["source_focus"] = [source_focus_enum]

        if TimeRange is not None:
            config_kwargs["time_range"] = get_time_range(time_range_id)

        config = ConversationConfig(**config_kwargs)
        
        # Reutiliza ou cria conversa
        if user_id in active_conversations:
             conversation = active_conversations[user_id]
             # Opcional: atualizar config da conversa existente se suportado
        else:
             conversation = active_client.create_conversation(config)
             active_conversations[user_id] = conversation

        def generate():
            # Retry loop para rotação de token
            max_retries = _get_auth_retry_limit()
            
            for attempt in range(max_retries):
                try:
                    # Recupera (ou recria) conversa dentro do loop para garantir cliente atualizado
                    current_client = get_active_client()
                    if user_id in active_conversations:
                        conversation = active_conversations[user_id]
                    else:
                        conversation = current_client.create_conversation(config)
                        active_conversations[user_id] = conversation

                    # Evento inicial
                    yield f"data: {json.dumps({'status': 'Iniciando busca...'})}\n\n"
                    
                    full_response = ""
                    citations = []
                    
                    conversation.ask(query, stream=True)
                    
                    last_thinking = ""
                    
                    for response_step in conversation:
                        # Extrai dados do passo
                        raw = response_step.raw_data
                        
                        # 1. Status/Thinking
                        thinking = None
                        if raw:
                            thinking = raw.get('thinking') or raw.get('reasoning')
                            # Se tiver steps (Sonar)
                            if not thinking and 'steps' in raw:
                                steps = raw.get('steps', [])
                                if steps:
                                    thinking = "\\n".join([s.get('content','') for s in steps if s.get('type')=='thinking'])

                        if thinking and thinking != last_thinking:
                            yield f"data: {json.dumps({'thinking': thinking})}\n\n"
                            last_thinking = thinking
                        
                        # 2. Citações (sources)
                        current_results = getattr(response_step, 'search_results', [])
                        if len(current_results) > len(citations):
                            for i in range(len(citations), len(current_results)):
                                src = current_results[i]
                                cit_data = {
                                    'title': getattr(src, 'title', 'Fonte'),
                                    'url': getattr(src, 'url', '')
                                }
                                yield f"data: {json.dumps({'citation': cit_data})}\n\n"
                            citations = current_results
                        
                        # 3. Chunk de Texto
                        current_answer = response_step.answer or ""
                        if len(current_answer) > len(full_response):
                            delta = current_answer[len(full_response):]
                            if delta:
                                yield f"data: {json.dumps({'chunk': delta})}\n\n"
                            full_response = current_answer
                            
                        # 4. Canvas / Files gerados
                        canvas_data = raw.get('canvas') or raw.get('blocks') or raw.get('generated_files')
                        if canvas_data:
                            logger.info(f"🎨 Canvas detected! Raw keys: {list(raw.keys())}")
                            logger.info(f"🎨 Canvas data type: {type(canvas_data).__name__}, preview: {json.dumps(canvas_data, default=str)[:500]}")
                            canvas_files = _extract_canvas_files(canvas_data)
                            for cf in canvas_files:
                                logger.info(f"📂 Emitting file: {cf['filename']} ({len(cf['content'])} chars)")
                                yield f"data: {json.dumps({'file': cf})}\n\n"

                        # 5. Clarifying Questions
                        # Alguns modelos retornam 'clarifying_question' boolean ou str
                        # Ou 'text' é uma pergunta.
                        # Vamos verificar se há flag explícita
                        if raw.get('clarifying_question'):
                            yield f"data: {json.dumps({'clarifying_question': True})}\n\n"
                            
                    # Se chegou aqui, sucesso total
                    # Final Payload
                    final_payload = {
                        "done": True,
                        "answer": full_response,
                        "citations": [{'title': getattr(c, 'title'), 'url': getattr(c, 'url')} for c in citations],
                        "conversation_id": user_id,
                        "backend_uuid": get_conversation_uuid(conversation)
                    }
                    yield f"data: {json.dumps(final_payload)}\n\n"
                    
                    # Salva histórico
                    if user_id not in conversation_messages:
                        conversation_messages[user_id] = []
                    conversation_messages[user_id].append({"role": "user", "content": query})
                    conversation_messages[user_id].append({"role": "assistant", "content": full_response})
                    conversation_message_counts[user_id] = conversation_message_counts.get(user_id, 0) + 1
                    
                    break # Break retry loop
                    
                except Exception as e:
                    err_str = str(e).lower()
                    is_auth = _is_auth_error(err_str)

                    if is_auth:
                        recovery_action = _recover_auth_failure(
                            user_id=user_id,
                            err_str=err_str,
                            attempt=attempt,
                            max_retries=max_retries,
                            route_name="/search_stream",
                        )
                        if recovery_action == "refresh":
                            yield f"data: {json.dumps({'status': '🔄 Sessão renovada automaticamente. Tentando novamente...'})}\n\n"
                            continue
                        if recovery_action == "rotate":
                            yield f"data: {json.dumps({'status': '🔄 Token expirado. Trocando conta...'})}\n\n"
                            continue
                             
                    logger.error(f"Erro stream final: {e}")
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    break

        return app.response_class(generate(), mimetype='text/event-stream')

    except Exception as e:
        logger.error(f"Erro /search_stream: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/models', methods=['GET'])
def list_models():
    if MODELS is not None:
        return jsonify({
            "models": [
                {
                    "id": model.id,
                    "name": model.name,
                    "description": model.description,
                    "tier": getattr(model, "min_tier", "pro"),
                    "mode": getattr(model, "mode", None),
                    "aliases": MODEL_ALIAS_GROUPS.get(model.id, []),
                }
                for model in MODELS.list_all()
            ],
            "focus_modes": [
                {"id": "web", "name": "Web", "description": "Busca geral na web"},
                {"id": "academic", "name": "Academic", "description": "Papers cientificos e academicos"},
                {"id": "social", "name": "Social", "description": "Reddit, Twitter e redes sociais"},
                {"id": "finance", "name": "Finance", "description": "Dados financeiros e SEC EDGAR"},
                {"id": "writing", "name": "Writing", "description": "Auxilio a escrita"},
            ],
            "citation_modes": [
                {"id": "default", "description": "texto[1] - Citacoes numeradas"},
                {"id": "markdown", "description": "texto[1](url) - Citacoes com links"},
                {"id": "clean", "description": "texto - Sem citacoes"},
            ],
        })

    """Lista modelos e focus modes disponíveis"""
    return jsonify({
        "models": [
            {"id": "best", "name": "🎯 Best (Auto)", "description": "Seleciona automaticamente o melhor modelo", "tier": "pro"},
            {"id": "sonar", "name": "⚡ Sonar", "description": "Modelo mais recente da Perplexity", "tier": "pro"},
            {"id": "deep-research", "name": "📊 Deep Research", "description": "Relatórios detalhados com mais fontes", "tier": "pro"},
            {"id": "gpt-5.4", "name": "🧠 GPT-5.4", "description": "Modelo mais recente da OpenAI", "tier": "pro"},
            {"id": "gpt-5.4-thinking", "name": "🧠💭 GPT-5.4 Thinking", "description": "GPT-5.4 com raciocínio", "tier": "pro"},
            {"id": "claude-sonnet-4.6", "name": "🎭 Claude Sonnet 4.6", "description": "Modelo rápido da Anthropic", "tier": "pro"},
            {"id": "claude-sonnet-4.6-thinking", "name": "🎭💭 Claude Sonnet 4.6 Think", "description": "Claude Sonnet 4.6 com raciocínio", "tier": "pro"},
            {"id": "claude-opus-4.6", "name": "🎭✨ Claude Opus 4.6", "description": "Modelo mais avançado da Anthropic", "tier": "max"},
            {"id": "claude-opus-4.6-thinking", "name": "🎭💭 Claude Opus 4.6 Think", "description": "Claude Opus 4.6 com raciocínio", "tier": "max"},
            {"id": "gemini-3-flash", "name": "💎 Gemini 3 Flash", "description": "Modelo rápido do Google", "tier": "pro"},
            {"id": "gemini-3-flash-thinking", "name": "💎💭 Gemini 3 Flash Think", "description": "Gemini 3 Flash com raciocínio", "tier": "pro"},
            {"id": "gemini-3.1-pro", "name": "💎🆕 Gemini 3.1 Pro", "description": "Modelo mais avançado do Google (NOVO!)", "tier": "pro"},
            {"id": "gemini-3.1-pro-thinking", "name": "💎💭 Gemini 3.1 Pro Think", "description": "Gemini 3.1 Pro com raciocínio", "tier": "pro"},
            {"id": "grok-4.1", "name": "🚀 Grok 4.1", "description": "Modelo mais recente da xAI", "tier": "pro"},
            {"id": "grok-4.1-thinking", "name": "🚀💭 Grok 4.1 Thinking", "description": "Grok 4.1 com raciocínio", "tier": "pro"},
            {"id": "kimi-k2.5-thinking", "name": "🌙 Kimi K2.5 Thinking", "description": "Modelo da Moonshot AI", "tier": "pro"},
            {"id": "nv-nemotron-3-super-thinking", "name": "🔬 Nemotron 3 Super", "description": "NVIDIA Nemotron 3 Super 120B com raciocínio (NOVO!)", "tier": "pro"}
        ],
        "focus_modes": [
            {"id": "web", "name": "🌐 Web", "description": "Busca geral na web"},
            {"id": "academic", "name": "🎓 Academic", "description": "Papers científicos e acadêmicos"},
            {"id": "social", "name": "💬 Social", "description": "Reddit, Twitter e redes sociais"},
            {"id": "finance", "name": "📈 Finance", "description": "Dados financeiros e SEC EDGAR"},
            {"id": "writing", "name": "✍️ Writing", "description": "Auxílio à escrita"}
        ],
        "citation_modes": [
            {"id": "default", "description": "texto[1] - Citações numeradas"},
            {"id": "markdown", "description": "texto[1](url) - Citações com links"},
            {"id": "clean", "description": "texto - Sem citações"}
        ]
    })


@app.route('/search', methods=['POST'])
@require_api_key
@limiter.limit("20 per minute")
def search():
    """
    Endpoint principal de busca COM HISTÓRICO NATIVO e retry com rotação de token.
    
    Payload:
    {
        "query": "string",
        "user_id": "string (identificador do usuário)",
        "model": "best|sonar|deep-research|gpt-5.4|claude-sonnet-4.6|claude-opus-4.6|gemini-3.1-pro|grok-4.1|nv-nemotron-3-super-thinking|...",
        "focus": "web|academic|social|finance|writing",
        "time_range": "all|day|week|month|year",
        "citation_mode": "default|markdown|clean"
    }
    
    O histórico é mantido automaticamente por user_id.
    Use POST /clear para limpar o histórico de um usuário.
    """
    try:
        # Suporte Híbrido: JSON ou Multipart/Form
        files_to_upload = []
        
        data = None
        if request.is_json:
            data = request.json
        else:
            data = request.form
            # Processa upload de arquivos
            if request.files:
                try:
                    upload_dir = Path(tempfile.gettempdir()) / "pplx_uploads"
                    upload_dir.mkdir(exist_ok=True)
                    
                    for key in request.files:
                        file = request.files[key]
                        if file.filename:
                            safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
                            tmp_path = upload_dir / safe_name
                            file.save(tmp_path)
                            files_to_upload.append(str(tmp_path))
                            logger.info(f"[UPLOAD] Arquivo salvo: {tmp_path}")
                except Exception as e:
                    logger.error(f"Erro ao salvar upload: {e}")

        if not data or 'query' not in data:
            return jsonify({"error": "Campo 'query' é obrigatório"}), 400
        
        # Validação com Pydantic (ignora se vier de multipart)
        if request.is_json:
            try:
                validated = SearchRequest(**data)
                query = validated.query
                user_id = validated.user_id
                model_id = validated.model
                focus_id = validated.focus
                time_range_id = validated.time_range
                citation_mode = validated.citation_mode
            except Exception as e:
                return jsonify({"error": f"Validação falhou: {e}"}), 400
        else:
            query = data['query']
            user_id = str(data.get('user_id', 'default'))
            model_id = data.get('model', 'best')
            focus_id = data.get('focus', 'web')
            time_range_id = data.get('time_range', 'all')
            citation_mode = data.get('citation_mode', 'markdown')
        
        # Verifica se o scraper está disponível
        if not SCRAPER_AVAILABLE:
            return jsonify({
                "error": "Scraper não instalado",
                "message": "Execute: pip install git+https://github.com/henrique-coder/perplexity-webui-scraper"
            }), 503
        
        # Verifica se o cliente foi inicializado
        active_client = get_active_client()
        if active_client is None:
            return jsonify({
                "error": "Cliente não inicializado",
                "message": "Verifique o PERPLEXITY_SESSION_TOKEN no arquivo .env"
            }), 503
        
        # Atualiza timestamp de atividade
        conversation_last_activity[user_id] = time.time()
        
        # Obtém enums
        model_enum = get_model_enum(model_id)
        citation_enum = get_citation_mode(citation_mode)
        
        # Retry loop para rotação de token
        max_retries = _get_auth_retry_limit()
        last_error = None
        
        for attempt in range(max_retries):
            try:
                active_client = get_active_client()
                
                # Verifica se já existe conversa ativa para este usuário
                conversation = active_conversations.get(user_id)
                is_new_conversation = False
                
                if conversation is None:
                    # Cria nova conversa para o usuário
                    config_kwargs = {
                        "model": model_enum,
                        "citation_mode": citation_enum,
                        "language": "pt-BR",
                        "save_to_library": SAVE_TO_LIBRARY_ENABLED,
                        "search_focus": get_search_focus(focus_id),
                    }
                    
                    # Adiciona time_range se disponível
                    if TimeRange is not None:
                        config_kwargs["time_range"] = get_time_range(time_range_id)
                    
                    # Localização fixa: Brasil (Brasília)
                    try:
                        from perplexity_webui_scraper import Coordinates
                        config_kwargs["coordinates"] = Coordinates(
                            latitude=-15.7801,
                            longitude=-47.9292,
                            accuracy=100.0
                        )
                    except Exception:
                        pass

                    # Adiciona source_focus apenas se disponível
                    if SourceFocus is not None:
                        source_focus_enum = get_source_focus(focus_id)
                        if source_focus_enum:
                            config_kwargs["source_focus"] = [source_focus_enum]
                    
                    config = ConversationConfig(**config_kwargs)
                    conversation = active_client.create_conversation(config)
                    active_conversations[user_id] = conversation
                    
                    # Se já tem mensagens em memória, injeta na nova conversa
                    if user_id in conversation_messages and conversation_messages[user_id]:
                        logger.info(f"[SEARCH] Restaurando {len(conversation_messages[user_id])} mensagens para user_id={user_id}")
                        try:
                            msgs = conversation_messages[user_id]
                            for i in range(0, len(msgs) - 1, 2):
                                user_msg = msgs[i]
                                asst_msg = msgs[i+1] if i+1 < len(msgs) else None
                                
                                if user_msg.get('role') == 'user' and asst_msg and asst_msg.get('role') == 'assistant':
                                     if hasattr(conversation, 'add_message'):
                                        conversation.add_message(user_msg['content'], role='user')
                                        conversation.add_message(asst_msg['content'], role='assistant')
                        except Exception as e:
                            logger.warning(f"Erro ao restaurar histórico nativo: {e}")
                    else:
                        conversation_messages[user_id] = []
                        
                    conversation_message_counts[user_id] = len(conversation_messages.get(user_id, []))
                    is_new_conversation = True
                    logger.info(f"[SEARCH] Nova conversa iniciada para user_id={user_id}")
                
                # Incrementa contador de mensagens
                conversation_message_counts[user_id] = conversation_message_counts.get(user_id, 0) + 1
                msg_count = conversation_message_counts[user_id]
                
                logger.info(f"[SEARCH] Query: {query[:50]}... | User: {user_id} | Msg #{msg_count} | Model: {model_id}")
                
                # Faz a pergunta NA MESMA CONVERSA
                if files_to_upload:
                    logger.info(f"[SEARCH] Enviando {len(files_to_upload)} arquivos para Perplexity...")
                    conversation.ask(query, files=files_to_upload)
                else:
                    conversation.ask(query)
                
                # Extrai resposta
                answer = conversation.answer if hasattr(conversation, 'answer') else str(conversation)
                
                # Salva mensagens para persistência
                if user_id not in conversation_messages:
                    conversation_messages[user_id] = []
                conversation_messages[user_id].append({"role": "user", "content": query})
                conversation_messages[user_id].append({"role": "assistant", "content": answer})
                
                # Extrai thinking (raciocínio) se disponível
                thinking = None
                raw_data = get_raw_data(conversation)
                
                if raw_data:
                    thinking = raw_data.get('thinking') or raw_data.get('reasoning') or raw_data.get('thought_process')
                    
                    if not thinking:
                        steps = raw_data.get('steps', [])
                        if steps and isinstance(steps, list):
                            thinking_steps = [s.get('content', '') for s in steps if s.get('type') in ['thinking', 'reasoning']]
                            if thinking_steps:
                                thinking = '\n'.join(thinking_steps)
                    
                    if not thinking:
                        thinking = raw_data.get('internal_reasoning')
                
                # Extrai citações se disponíveis
                citations = []
                search_results = getattr(conversation, 'search_results', []) if hasattr(conversation, 'search_results') else []
                if search_results:
                    for src in search_results:
                        citations.append({
                            "title": getattr(src, 'title', 'Fonte'),
                            "url": getattr(src, 'url', ''),
                            "snippet": getattr(src, 'snippet', '')
                        })
                
                # Fallback para sources (versões antigas)
                if not citations and hasattr(conversation, 'sources') and conversation.sources:
                    for src in conversation.sources:
                        citations.append({
                            "title": getattr(src, 'title', 'Fonte'),
                            "url": getattr(src, 'url', ''),
                            "snippet": getattr(src, 'snippet', '')
                        })
                
                response = {
                    "status": "success",
                    "answer": answer,
                    "thinking": thinking,
                    "model_used": model_id,
                    "focus_mode": focus_id,
                    "time_range": time_range_id,
                    "citations": citations,
                    "has_thinking": thinking is not None,
                    "conversation_info": {
                        "id": user_id,
                        "uuid": get_conversation_uuid(conversation),
                        "model": model_id,
                        "message_count": conversation_message_counts.get(user_id, 0)
                    }
                }

                # Limpeza de arquivos temporários
                for fpath in files_to_upload:
                    try:
                        os.remove(fpath)
                    except Exception as e:
                        logger.warning(f"Erro ao remover temp {fpath}: {e}")
                
                return jsonify(response)
            
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_auth = _is_auth_error(err_str)

                if is_auth:
                    recovery_action = _recover_auth_failure(
                        user_id=user_id,
                        err_str=err_str,
                        attempt=attempt,
                        max_retries=max_retries,
                        route_name="/search",
                    )
                    if recovery_action:
                        continue
                 
                logger.error(f"Erro em /search: {e}")
                logger.error(traceback.format_exc())
                
                # Limpeza de arquivos temporários mesmo em caso de erro
                for fpath in files_to_upload:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
                
                return jsonify({"error": str(e)}), 500
        
        # Se esgotou os retries
        return jsonify({"error": f"Falha após {max_retries} tentativas: {last_error}"}), 500
    
    except Exception as e:
        logger.error(f"Erro inesperado em /search: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route('/last_response', methods=['GET'])
@require_api_key
def get_last_response():
    """Retorna a última resposta gerada para o usuário (Retry)"""
    user_id = request.args.get('user_id', 'default')
    
    if user_id not in conversation_messages:
        return jsonify({"error": "No history found"}), 404
        
    messages = conversation_messages[user_id]
    if not messages:
        return jsonify({"error": "Empty history"}), 404
        
    # Procura a última msg do assistant de trás pra frente
    for msg in reversed(messages):
        if msg['role'] == 'assistant':
            return jsonify({
                "answer": msg['content'],
            })
            
    return jsonify({"error": "No assistant message found"}), 404


@app.route('/clear', methods=['POST'])
@require_api_key
def clear_conversation():
    """
    Limpa o histórico de conversa de um usuário.
    
    Payload:
    {
        "user_id": "string"
    }
    """
    try:
        data = request.json or {}
        user_id = str(data.get('user_id', 'default'))
        
        saved_id = None
        msg_count = 0
        
        # SALVA a conversa antes de limpar!
        if user_id in conversation_messages and conversation_messages[user_id]:
            saved_id = save_conversation(user_id)
            msg_count = len(conversation_messages[user_id]) // 2  # Pares de msgs
        
        # Limpa da memória
        if user_id in active_conversations:
            del active_conversations[user_id]
        if user_id in conversation_message_counts:
            del conversation_message_counts[user_id]
        if user_id in conversation_messages:
            del conversation_messages[user_id]
        if user_id in conversation_last_activity:
            del conversation_last_activity[user_id]
        
        if saved_id:
            logger.info(f"[CLEAR] Conversa salva como {saved_id} e limpa para user_id={user_id}")
            return jsonify({
                "success": True,
                "message": f"Conversa salva ({msg_count} mensagens) e limpa",
                "user_id": user_id,
                "saved_conversation_id": saved_id
            })
        else:
            return jsonify({
                "success": True,
                "message": "Nenhum histórico encontrado",
                "user_id": user_id
            })
            
    except Exception as e:
        logger.error(f"Erro em /clear: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/conversation-status', methods=['GET'])
@require_api_key
def conversation_status():
    """
    Retorna status das conversas ativas.
    Query param: ?user_id=xxx para ver de um usuário específico
    """
    user_id = request.args.get('user_id')
    
    if user_id:
        return jsonify({
            "user_id": user_id,
            "has_active_conversation": user_id in active_conversations,
            "message_count": conversation_message_counts.get(user_id, 0)
        })
    
    return jsonify({
        "total_active_conversations": len(active_conversations),
        "conversations": {
            uid: {"message_count": conversation_message_counts.get(uid, 0)}
            for uid in active_conversations.keys()
        }
    })


@app.route('/history/list', methods=['GET'])
@require_api_key
def history_list():
    """
    Lista conversas salvas de um usuário.
    Query param: ?user_id=xxx
    """
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    
    conversations = list_saved_conversations(user_id)
    return jsonify({"conversations": conversations})


@app.route('/history/load', methods=['POST'])
@require_api_key
def history_load():
    """
    Carrega uma conversa salva.
    Payload: { "user_id": "xxx", "conversation_id": "xxx" }
    """
    data = request.json or {}
    user_id = data.get('user_id')
    conv_id = data.get('conversation_id')
    
    if not user_id or not conv_id:
        return jsonify({"error": "user_id and conversation_id required"}), 400
        
    conversation_data = load_conversation(user_id, conv_id)
    if not conversation_data:
        return jsonify({"error": "Conversation not found"}), 404
    
    # Restaura estado em memória
    messages = conversation_data.get('messages', [])
    conversation_messages[user_id] = messages
    conversation_message_counts[user_id] = len(messages)
    
    # Remove conversa ativa anterior para forçar criação de nova com nosso histórico
    if user_id in active_conversations:
        del active_conversations[user_id]
    
    logger.info(f"[LOAD] Conversa {conv_id} carregada para user_id={user_id} ({len(messages)} msgs)")
    
    return jsonify({
        "success": True, 
        "conversation": conversation_data,
        "message": "Conversa carregada! Próxima mensagem continuará este contexto."
    })


@app.route('/history/delete', methods=['POST'])
@require_api_key
def history_delete():
    """
    Deleta uma conversa salva.
    Payload: { "user_id": "xxx", "conversation_id": "xxx" }
    """
    data = request.json or {}
    user_id = data.get('user_id')
    conv_id = data.get('conversation_id')
    
    if not user_id or not conv_id:
        return jsonify({"error": "user_id and conversation_id required"}), 400
        
    success = delete_saved_conversation(user_id, conv_id)
    return jsonify({"success": success})


@app.route('/config/library', methods=['GET', 'POST'])
@require_api_key
def config_library():
    """
    GET: Retorna estado atual do save_to_library
    POST: Inverte estado (toggle) e retorna novo
    """
    global SAVE_TO_LIBRARY_ENABLED
    
    if request.method == 'POST':
        SAVE_TO_LIBRARY_ENABLED = not SAVE_TO_LIBRARY_ENABLED
        logger.info(f"[CONFIG] Save to Library alterado para: {SAVE_TO_LIBRARY_ENABLED}")
        
    return jsonify({
        "enabled": SAVE_TO_LIBRARY_ENABLED,
        "message": "Save to Library ATIVADO" if SAVE_TO_LIBRARY_ENABLED else "Save to Library DESATIVADO"
    })


@app.route('/config/token', methods=['POST'])
@require_api_key
def config_token():
    """
    Atualiza o token de sessão do Perplexity em tempo de execução.
    Payload: {"token": "seu_token_aqui"}
    """
    global PERPLEXITY_SESSION_TOKEN
    
    data = request.json or {}
    
    try:
        validated = TokenUpdateRequest(**data)
    except Exception as e:
        return jsonify({"error": f"Validação falhou: {e}"}), 400
        
    try:
        # Reinicializa o cliente
        if SCRAPER_AVAILABLE and Perplexity:
            PERPLEXITY_SESSION_TOKEN = validated.token
            client_manager.init_default(validated.token)
            reset_runtime_conversations(save_existing=True)
            logger.info("✅ Cliente Perplexity reinicializado com NOVO token!")
            return jsonify({"success": True, "message": "Token atualizado e cliente reinicializado!"})
        else:
            return jsonify({"error": "Scraper not available"}), 503
            
    except Exception as e:
        logger.error(f"Erro ao atualizar token: {e}")
        return jsonify({"error": str(e)}), 500





@app.route('/vision', methods=['POST'])
@require_api_key
@limiter.limit("10 per minute")
def vision():
    """
    Endpoint para análise de imagens/arquivos.
    
    Payload:
    {
        "query": "string",
        "image_base64": "string (base64 encoded)",
        "model": "best" (recomendado)
    }
    """
    try:
        data = request.json
        
        if not data or 'query' not in data or 'image_base64' not in data:
            return jsonify({"error": "Campos 'query' e 'image_base64' são obrigatórios"}), 400
        
        # Validação Pydantic
        try:
            validated = VisionRequest(**data)
        except Exception as e:
            return jsonify({"error": f"Validação falhou: {e}"}), 400
        
        logger.info(f"[VISION] Query: {validated.query[:50]}... | Model: {validated.model}")
        
        # Verifica scraper
        active_client = get_active_client()
        if not SCRAPER_AVAILABLE or active_client is None:
            return jsonify({
                "error": "Scraper não disponível",
                "message": "Verifique a instalação e o session token"
            }), 503
        
        # Salva imagem temporariamente
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
            tmp.write(base64.b64decode(validated.image_base64))
            tmp_path = tmp.name
        
        try:
            # Cria configuração
            model_enum = get_model_enum(validated.model)
            config = ConversationConfig(
                model=model_enum,
                language="pt-BR"
            )
            
            # Cria conversa com arquivo
            conversation = active_client.create_conversation(config)
            conversation.ask(validated.query, files=[tmp_path])
            
            answer = conversation.answer if hasattr(conversation, 'answer') else str(conversation)
            
            return jsonify({"answer": answer})
            
        finally:
            # Remove arquivo temporário
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        
    except Exception as e:
        logger.error(f"Erro em /vision: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route('/diagnostics', methods=['GET'])
def diagnostics():
    """
    Diagnóstico completo do sistema.
    Checa:
    1. IP Público (para validar VPN)
    2. Autenticação Perplexity (tenta conectar)
    """
    result = {
        "mcp_status": "online",
        "public_ip": "unknown",
        "perplexity_auth": "unknown",
        "auth_error": None
    }
    
    # 1. Checa IP Público
    try:
        import urllib.request
        try:
            with urllib.request.urlopen('https://api.ipify.org', timeout=5) as response:
                result['public_ip'] = response.read().decode('utf-8')
        except:
             # Fallback
             with urllib.request.urlopen('https://ifconfig.me/ip', timeout=5) as response:
                result['public_ip'] = response.read().decode('utf-8').strip()
             
    except Exception as e:
        result['public_ip'] = f"Error: {str(e)}"
        
    # 2. Checa Autenticação Perplexity
    try:
        active_client = get_active_client()
        if SCRAPER_AVAILABLE and active_client is not None:
             # Verifica apenas se o cliente existe (o check anterior de .session falhava)
             result['perplexity_auth'] = "configured"
        else:
            result['perplexity_auth'] = "scraper_unavailable"

    except Exception as e:
        result['error'] = str(e)
        
    return jsonify(result)


# ============= ERROR HANDLERS =============

@app.errorhandler(429)
def ratelimit_handler(e):
    """Handler para rate limit excedido"""
    return jsonify({
        "error": "Rate limit excedido. Aguarde um momento.",
        "retry_after": e.description
    }), 429


@app.errorhandler(500)
def internal_error(e):
    """Handler para erros internos"""
    logger.error(f"Erro interno: {e}")
    return jsonify({"error": "Erro interno do servidor"}), 500


# ============= MAIN =============

# Flag para controle de shutdown
_shutdown_in_progress = False


def graceful_shutdown(signum=None, frame=None):
    """Handler para shutdown gracioso - salva dados antes de encerrar"""
    global _shutdown_in_progress
    
    if _shutdown_in_progress:
        return
    
    _shutdown_in_progress = True
    logger.info("🛑 Recebido sinal de shutdown...")
    
    # Salva conversas pendentes
    saved_count = 0
    for user_id in list(conversation_messages.keys()):
        if conversation_messages[user_id]:
            try:
                save_conversation(user_id)
                saved_count += 1
            except Exception as e:
                logger.warning(f"Erro ao salvar conversa {user_id}: {e}")
    
    if saved_count > 0:
        logger.info(f"💾 {saved_count} conversa(s) salva(s) antes do shutdown")
    
    logger.info("✅ Shutdown gracioso completo")
    
    # Força saída
    import sys
    sys.exit(0)


# Registra handlers de sinal
import signal
signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)


if __name__ == '__main__':
    logger.info(f"🚀 MCP Server iniciando na porta {MCP_PORT}")
    logger.info(f"📦 Scraper disponível: {SCRAPER_AVAILABLE}")
    logger.info(f"🔑 Cliente inicializado: {get_active_client() is not None}")
    logger.info(f"📍 SourceFocus disponível: {SourceFocus is not None}")
    logger.info(f"🔐 API Key: {'Configurada' if MCP_API_KEY else 'DESATIVADA (dev mode)'}")
    logger.info(f"🧹 Cleanup: TTL={CONVERSATION_TTL_SECONDS}s, Max={MAX_ACTIVE_CONVERSATIONS}")
    
    if not SCRAPER_AVAILABLE:
        logger.warning("⚠️ Instale o scraper: pip install git+https://github.com/henrique-coder/perplexity-webui-scraper")
    
    if get_active_client() is None and SCRAPER_AVAILABLE:
        logger.warning("⚠️ Configure o PERPLEXITY_SESSION_TOKEN no arquivo .env")
    
    if not MCP_API_KEY:
        logger.warning("⚠️ MCP_API_KEY não configurada! Endpoints acessíveis sem autenticação.")
    
    app.run(
        host='0.0.0.0',
        port=MCP_PORT,
        debug=os.getenv('FLASK_ENV') != 'production'
    )
