"""
Perplexo Bot - Telegram
=======================
Bot completo para Telegram com UI visual nativa:
- Menu de comandos com /
- InlineKeyboard com botões
- Seletores com checkmarks (✅/○)
- Toggles ON/OFF no /config
- Handlers para texto, imagens e documentos

Comandos:
- /start   - Menu principal
- /modelos - Escolher modelo AI
- /busca   - Modo de busca (Focus)
- /normal  - Conversa casual
- /config  - Configurações avançadas
- /ajuda   - Guia de uso

Uso:
    python src/telegram_bot.py
"""

import os
import sys
import base64
import logging
import asyncio
import json
import re
import time
from typing import Dict, Any, Optional

# APScheduler para tarefas agendadas
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    AsyncIOScheduler = None

# Task Manager local
from task_manager import Task, init_task_manager, get_task_manager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
import httpx
import tempfile
from dotenv import load_dotenv

# Carrega .env
load_dotenv()

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# TTS Service (Inworld AI) - importado APÓS logger ser criado
try:
    from tts_service import (
        tts_extract_and_generate, tts_extract_simple_response, tts_status, TTS_ENABLED,
        tts_fetch_voices, tts_get_voice_name, tts_generate_audio,
        TTS_IDIOMAS, DEFAULT_VOICE as TTS_DEFAULT_VOICE
    )
    TTS_AVAILABLE = True
    logger.info(f"✅ TTS Service carregado: enabled={TTS_ENABLED}")
except Exception as _tts_import_err:
    TTS_AVAILABLE = False
    TTS_ENABLED = False
    logger.warning(f"⚠️ TTS service não disponível: {_tts_import_err}")

# ============= CONFIGURAÇÃO =============

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
MCP_API = os.getenv("MCP_API_URL", "http://127.0.0.1:5000")
VPN_API = os.getenv("VPN_API_URL", "http://127.0.0.1:8000")  # Gluetun control server
VPN_ENABLED = os.getenv("VPN_ENABLED", "true").lower() in ("true", "1", "yes")
OPENVPN_USER = os.getenv("OPENVPN_USER", "")
OPENVPN_PASSWORD = os.getenv("OPENVPN_PASSWORD", "")
TELEGRAM_PORT = int(os.getenv("TELEGRAM_PORT", 8000))
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

# Controle de acesso: lista de user IDs autorizados (vazio = todos permitidos)
_allowed_raw = os.getenv("ALLOWED_TELEGRAM_USERS", "")
ALLOWED_TELEGRAM_USERS = [int(uid.strip()) for uid in _allowed_raw.split(",") if uid.strip().isdigit()]


def _mcp_headers() -> dict:
    """Retorna headers padrão para chamadas ao MCP, incluindo API key se configurada."""
    headers = {}
    if MCP_API_KEY:
        headers["X-API-Key"] = MCP_API_KEY
    return headers


def _check_access(user_id: int) -> bool:
    """Retorna True se o user_id é autorizado (lista vazia = todos permitidos)."""
    if not ALLOWED_TELEGRAM_USERS:
        return True
    return user_id in ALLOWED_TELEGRAM_USERS

# Verifica token
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "seu_token_aqui":
    logger.warning("⚠️ TELEGRAM_TOKEN não configurado! Edite o arquivo .env")
    logger.warning("⚠️ Bot não será iniciado. Configure TELEGRAM_TOKEN no .env")

# ============= STORAGE DE PREFERÊNCIAS =============
# Em produção, substitua por Redis ou banco de dados

user_preferences: Dict[int, Dict[str, Any]] = {}

# Buffer de arquivos pendentes (até 9 arquivos por usuário)
# Formato: {user_id: [{"name": str, "bytes": bytes, "mime": str, "timestamp": float}, ...]}
pending_files: Dict[int, list] = {}

# Versão do bot
BOT_VERSION = "v2.0.87"


def get_user_config(user_id: int) -> Dict[str, Any]:
    """Retorna configuração do usuário ou padrão"""
    return user_preferences.get(user_id, {
        'model': 'sonar',
        'focus': 'web',
        'mode': 'busca',
        'time_range': 'all',
        'reasoning': False,
        'return_images': True,
        'return_citations': True
    })


def save_user_config(user_id: int, config: Dict[str, Any]) -> None:
    """Salva configuração do usuário"""
    user_preferences[user_id] = config


# ============= DADOS DOS MODELOS E FOCUS =============

FALLBACK_MODELS = [
    {"id": "perplexity/best", "name": "Best", "tier": "pro", "mode": "search"},
    {"id": "perplexity/sonar-2", "name": "Sonar 2", "tier": "pro", "mode": "copilot"},
    {"id": "perplexity/deep-research", "name": "Deep Research", "tier": "pro", "mode": "research"},
    {"id": "openai/gpt-5.4", "name": "GPT-5.4", "tier": "pro", "mode": "copilot"},
    {"id": "openai/gpt-5.4-thinking", "name": "GPT-5.4 Thinking", "tier": "pro", "mode": "copilot"},
    {"id": "openai/gpt-5.5-thinking", "name": "GPT-5.5 Thinking", "tier": "max", "mode": "copilot"},
    {"id": "google/gemini-3.1-pro-thinking-low", "name": "Gemini 3.1 Low", "tier": "pro", "mode": "copilot"},
    {"id": "google/gemini-3.1-pro-thinking-high", "name": "Gemini 3.1 High", "tier": "pro", "mode": "copilot"},
    {"id": "anthropic/claude-sonnet-4.6", "name": "Claude Sonnet 4.6", "tier": "pro", "mode": "copilot"},
    {"id": "anthropic/claude-sonnet-4.6-thinking", "name": "Claude Sonnet Think", "tier": "pro", "mode": "copilot"},
    {"id": "anthropic/claude-opus-4.7", "name": "Claude Opus 4.7", "tier": "max", "mode": "copilot"},
    {"id": "anthropic/claude-opus-4.7-thinking", "name": "Claude Opus Think", "tier": "max", "mode": "copilot"},
    {"id": "moonshot/kimi-k2.6-instant", "name": "Kimi K2.6 Instant", "tier": "pro", "mode": "copilot"},
    {"id": "moonshot/kimi-k2.6-thinking", "name": "Kimi K2.6 Thinking", "tier": "pro", "mode": "copilot"},
    {"id": "nvidia/nemotron-3-super-thinking", "name": "Nemotron 3 Super", "tier": "pro", "mode": "copilot"},
]

MODEL_ALIASES = {
    "best": "perplexity/best",
    "sonar": "perplexity/sonar-2",
    "sonar-2": "perplexity/sonar-2",
    "deep-research": "perplexity/deep-research",
    "gpt-5.2": "openai/gpt-5.4",
    "gpt-5.2-thinking": "openai/gpt-5.4-thinking",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-thinking": "openai/gpt-5.4-thinking",
    "gpt-5.5-thinking": "openai/gpt-5.5-thinking",
    "claude-sonnet-4.6": "anthropic/claude-sonnet-4.6",
    "claude-4.6-sonnet": "anthropic/claude-sonnet-4.6",
    "claude-sonnet-4.6-thinking": "anthropic/claude-sonnet-4.6-thinking",
    "claude-opus-4.6": "anthropic/claude-opus-4.7",
    "claude-4.6-opus": "anthropic/claude-opus-4.7",
    "claude-opus-4.7": "anthropic/claude-opus-4.7",
    "claude-opus-4.7-thinking": "anthropic/claude-opus-4.7-thinking",
    "gemini-3-flash": "google/gemini-3.1-pro-thinking-low",
    "gemini-3-flash-thinking": "google/gemini-3.1-pro-thinking-high",
    "gemini-3.1-pro": "google/gemini-3.1-pro-thinking-low",
    "gemini-3.1-pro-thinking": "google/gemini-3.1-pro-thinking-high",
    "grok-4.1": "perplexity/sonar-2",
    "grok-4.1-thinking": "openai/gpt-5.5-thinking",
    "kimi-k2.5-thinking": "moonshot/kimi-k2.6-thinking",
    "kimi-k2.6-instant": "moonshot/kimi-k2.6-instant",
    "kimi-k2.6-thinking": "moonshot/kimi-k2.6-thinking",
    "nemotron": "nvidia/nemotron-3-super-thinking",
}

MODEL_EMOJIS = {
    "perplexity": "🔎",
    "openai": "🧠",
    "google": "💎",
    "anthropic": "🎭",
    "moonshot": "🌙",
    "nvidia": "🔬",
}

_models_cache = {"items": None, "at": 0.0}
_model_selection_cache: Dict[int, Dict[str, str]] = {}


def _canonical_model_id(model_id: str) -> str:
    normalized = (model_id or "perplexity/best").strip().lower().replace("_", "-")
    return MODEL_ALIASES.get(normalized, normalized)


def _model_emoji(model_id: str) -> str:
    provider = (model_id or "").split("/", 1)[0]
    return MODEL_EMOJIS.get(provider, "🤖")


def _short_model_name(model: dict) -> str:
    model_id = model.get("id", "")
    name = model.get("name") or model_id.rsplit("/", 1)[-1]
    replacements = {
        "Deep research": "Deep Research",
        "Gemini 3.1 Pro Thinking Low": "Gemini 3.1 Low",
        "Gemini 3.1 Pro Thinking High": "Gemini 3.1 High",
        "Claude Sonnet 4.6 Thinking": "Claude Sonnet Think",
        "Claude Opus 4.7 Thinking": "Claude Opus Think",
        "Kimi K2.6 Thinking": "Kimi Thinking",
        "Kimi K2.6 Instant": "Kimi Instant",
        "Nemotron 3 Super Thinking": "Nemotron Think",
    }
    return replacements.get(name, name)


def _model_button_label(model: dict, current_model: str) -> str:
    model_id = model.get("id", "")
    selected = _canonical_model_id(current_model) == model_id
    tier = str(model.get("tier") or model.get("min_tier") or "pro").lower()
    tier_label = " MAX" if tier == "max" else ""
    return f"{'✅ ' if selected else ''}{_model_emoji(model_id)} {_short_model_name(model)}{tier_label}"


def _model_detail_line(model: dict, current_model: str) -> str:
    model_id = model.get("id", "")
    marker = "✅" if _canonical_model_id(current_model) == model_id else "○"
    tier = str(model.get("tier") or model.get("min_tier") or "pro").upper()
    mode = model.get("mode") or "copilot"
    return f"{marker} {_model_emoji(model_id)} *{_short_model_name(model)}* · `{tier}` · _{mode}_"


async def get_available_models() -> list:
    now = time.time()
    cached = _models_cache.get("items")
    if cached and now - float(_models_cache.get("at", 0)) < 300:
        return cached

    try:
        async with httpx.AsyncClient(timeout=8.0, headers=_mcp_headers()) as client:
            response = await client.get(f"{MCP_API}/models")
            response.raise_for_status()
            data = response.json()
            models = data.get("models") or []
            normalized = []
            for model in models:
                model_id = model.get("id")
                if not model_id:
                    continue
                normalized.append({
                    "id": model_id,
                    "name": model.get("name") or model_id.rsplit("/", 1)[-1],
                    "tier": model.get("tier") or model.get("min_tier") or "pro",
                    "mode": model.get("mode") or "copilot",
                })
            if normalized:
                _models_cache["items"] = normalized
                _models_cache["at"] = now
                return normalized
    except Exception as e:
        logger.warning(f"Não foi possível carregar modelos da API, usando fallback: {e}")

    _models_cache["items"] = FALLBACK_MODELS
    _models_cache["at"] = now
    return FALLBACK_MODELS

def _resolve_model_id(base_model: str, thinking: bool) -> str:
    """
    Retorna o ID real do modelo baseado no toggle de Reasoning.
    Ex: claude-4.6-sonnet + thinking=True -> claude-4.6-sonnet-thinking
    """
    base_model = _canonical_model_id(base_model)
    if not thinking:
        return base_model
        
    # Mapeamento de Thinking
    mapping = {
        'openai/gpt-5.4': 'openai/gpt-5.4-thinking',
        'anthropic/claude-sonnet-4.6': 'anthropic/claude-sonnet-4.6-thinking',
        'anthropic/claude-opus-4.7': 'anthropic/claude-opus-4.7-thinking',
        'google/gemini-3.1-pro-thinking-low': 'google/gemini-3.1-pro-thinking-high',
        'moonshot/kimi-k2.6-instant': 'moonshot/kimi-k2.6-thinking',
    }
    
    return mapping.get(base_model, base_model)

FOCUS_MODES = [
    ('web', '🌐 Web', 'Busca geral'),
    ('academic', '🎓 Academic', 'Papers científicos'),
    ('writing', '✍️ Writing', 'Auxílio escrita'),
    ('youtube', '🎥 YouTube', 'Vídeos'),
    ('reddit', '💬 Reddit', 'Discussões'),
    ('wolfram', '🧮 Wolfram', 'Matemática/Cálculos')
]

TIME_RANGES = [
    ('all', '♾️ Qualquer data', 'Sem filtro de tempo'),
    ('day', '📅 Últimas 24h', 'Pesquisa apenas hoje'),
    ('week', '🗓️ Esta Semana', 'Últimos 7 dias'),
    ('month', '📆 Este Mês', 'Últimos 30 dias'),
    ('year', '📅 Este Ano', 'Últimos 365 dias')
]


# ============= COMANDO /start =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu principal com botões inline"""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    keyboard = [
        [
            InlineKeyboardButton("✨ Nova Conversa", callback_data='menu_new'),
            InlineKeyboardButton("📂 Histórico", callback_data='menu_history')
        ],
        [
            InlineKeyboardButton("🤖 Modelo", callback_data='menu_modelos'),
            InlineKeyboardButton("🔍 Busca", callback_data='menu_busca'),
            InlineKeyboardButton("⏱ Tempo", callback_data='menu_tempo')
        ],
        [
            InlineKeyboardButton("🔑 Tokens", callback_data='menu_tokens'),
            InlineKeyboardButton("🔒 VPN", callback_data='menu_vpn'),
            InlineKeyboardButton("☁️ Library", callback_data='menu_library')
        ],
        [
            InlineKeyboardButton("📅 Tarefas", callback_data='menu_tasks'),
            InlineKeyboardButton("⚙️ Config", callback_data='menu_config'),
            InlineKeyboardButton("❓ Ajuda", callback_data='menu_ajuda')
        ]
    ]
    
    # Alerta de VPN sem credenciais
    vpn_warning = ""
    if VPN_ENABLED and (not OPENVPN_USER or not OPENVPN_PASSWORD):
        vpn_warning = (
            "\n⚠️ *ALERTA VPN:* Credenciais não configuradas!\n"
            "Configure `OPENVPN_USER` e `OPENVPN_PASSWORD` no `.env`\n"
            "A VPN não vai funcionar sem essas credenciais.\n"
        )

    text = (
        f"🌀 *Perplexo Bot {BOT_VERSION}* — Painel de Controle\n\n"
        f"*Status Atual:*\n"
        f"🤖 Modelo: `{_canonical_model_id(config['model'])}`\n"
        f"🔍 Focus: `{config['focus']}`\n"
        f"💬 Modo: `{config['mode']}`\n"
        f"☁️ Library: `{'ON' if config.get('save_to_library') else 'OFF'}`"
        f"{vpn_warning}\n"
        f"_Selecione uma opção:_"
    )
    
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif update.callback_query:
        # Se for o mesmo texto, ignora erro de edição
        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        except Exception:
            # Às vezes o conteúdo é idêntico
            pass


# ============= COMANDO /modelos =============

async def cmd_modelos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista de modelos reais da API com seletor visual."""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    current_model = _canonical_model_id(config['model'])
    models = await get_available_models()
    _model_selection_cache[user_id] = {str(i): model["id"] for i, model in enumerate(models)}
    
    keyboard = []
    row = []
    for i, model in enumerate(models):
        row.append(InlineKeyboardButton(
            _model_button_label(model, current_model),
            callback_data=f'modelsel_{i}'
        ))
        
        if len(row) == 2:
            keyboard.append(row)
            row = []
            
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("« Voltar", callback_data='back_main')])
    
    text = (
        "🤖 *Escolher Modelo AI*\n\n"
        f"Atual: `{current_model}`\n\n"
    )
    for model in models:
        text += _model_detail_line(model, current_model) + "\n"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )


# ============= COMANDO /busca =============

async def cmd_busca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Focus modes com seletor visual"""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    current_focus = config['focus']
    
    keyboard = []
    for focus_id, emoji_name, description in FOCUS_MODES:
        prefix = "✅ " if focus_id == current_focus else ""
        button_text = f"{prefix}{emoji_name}"
        keyboard.append([InlineKeyboardButton(
            button_text,
            callback_data=f'set_focus_{focus_id}'
        )])
    

    keyboard.append([InlineKeyboardButton("📅 Recency (Tempo)", callback_data='menu_tempo')])
    keyboard.append([InlineKeyboardButton("« Voltar", callback_data='back_main')])
    
    text = "🔍 *Modo de Busca (Focus)*\n\n"
    for focus_id, emoji_name, description in FOCUS_MODES:
        marker = "✅" if focus_id == current_focus else "○"
        text += f"{marker} *{emoji_name}* - {description}\n"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )


async def cmd_tempo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Seletor de Time Range"""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    current_range = config.get('time_range', 'all')
    
    keyboard = []
    for range_id, emoji_name, description in TIME_RANGES:
        prefix = "✅ " if range_id == current_range else ""
        button_text = f"{prefix}{emoji_name}"
        keyboard.append([InlineKeyboardButton(
            button_text,
            callback_data=f'set_time_{range_id}'
        )])
    
    keyboard.append([InlineKeyboardButton("« Voltar", callback_data='menu_busca')])
    
    text = "📅 *Filtro de Tempo (Recency)*\n\n"
    for range_id, emoji_name, description in TIME_RANGES:
        marker = "✅" if range_id == current_range else "○"
        text += f"{marker} *{emoji_name}* - {description}\n"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )


# ============= COMANDO /config =============

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Painel de configurações com toggle switches"""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    keyboard = [
        [InlineKeyboardButton(
            f"🧠 Reasoning: {'🟢 ON' if config['reasoning'] else '🔴 OFF'}",
            callback_data='toggle_reasoning'
        )],
        [InlineKeyboardButton(
            f"📚 Citações: {'🟢 ON' if config['return_citations'] else '🔴 OFF'}",
            callback_data='toggle_citations'
        )],
        [InlineKeyboardButton(
            f"🖼️ Imagens: {'🟢 ON' if config['return_images'] else '🔴 OFF'}",
            callback_data='toggle_images'
        )],
        [
            InlineKeyboardButton("🤖 Modelo", callback_data='menu_modelos'),
            InlineKeyboardButton("🔍 Focus", callback_data='menu_busca')
        ],
        [InlineKeyboardButton("« Voltar", callback_data='back_main')]
    ]
    
    text = (
        "⚙️ *Configurações*\n\n"
        f"*Modelo Atual:* `{_canonical_model_id(config['model'])}`\n"
        f"*Focus Atual:* `{config['focus']}`\n"
        f"*Modo:* `{config['mode']}`\n\n"
        f"*Opções Avançadas:*\n"
        f"{'🟢' if config['reasoning'] else '🔴'} Reasoning (raciocínio step-by-step)\n"
        f"{'🟢' if config['return_citations'] else '🔴'} Citações de fontes\n"
        f"{'🟢' if config['return_images'] else '🔴'} Retornar imagens\n\n"
        f"_Toque nos botões para alternar ON/OFF_"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )


# ============= COMANDO /normal =============

async def cmd_normal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ativa modo conversa normal"""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    config['mode'] = 'normal'
    config['return_citations'] = False
    save_user_config(user_id, config)
    
    text = (
        "💬 *Modo Normal ativado*\n\n"
        "Agora respondo sem citações, como uma conversa casual.\n"
        "Use /busca para voltar ao modo pesquisa."
    )
    
    if update.callback_query:
        await update.callback_query.answer("Modo normal ativado!")
        await update.callback_query.edit_message_text(text, parse_mode='Markdown')
    elif update.message:
        await update.message.reply_text(text, parse_mode='Markdown')


# ============= COMANDO /ajuda =============

async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Guia de uso"""
    text = (
        "❓ *Guia de Uso do Perplexo Bot*\n\n"
        "*Comandos no menu '/' :*\n"
        "• `/start` - Menu principal\n"
        "• `/modelos` - Escolher modelo AI\n"
        "• `/busca` - Modo de busca (Focus)\n"
        "• `/normal` - Conversa casual\n"
        "• `/config` - Configurações avançadas\n"
        "• `/limpar` - Limpar histórico de conversa\n\n"
        "*Recursos:*\n"
        "• Envie texto para perguntas\n"
        "• Envie imagens para análise visual\n"
        "• Envie arquivos .txt para resumir\n\n"
        "*Histórico:*\n"
        "💬 Conversas salvas automaticamente ao usar `/new` ou `/limpar`.\n"
        "• `/historico` - Lista conversas antigas\n"
        "• `/importar <ID>` - Importa conversa antiga como contexto atual\n\n"
        "*Modelos disponíveis:*\n"
        "⚡ Sonar - Rápido, ideal para Q&A\n"
        "🔥 Sonar Pro - Análises detalhadas\n"
        "🧠 GPT-5.2 - Coding e raciocínio\n"
        "🤔 Reasoning Pro - Lógica complexa\n"
        "📊 Deep Research - Pesquisa máxima\n\n"
        "*Dica:* Use os botões do `/config` para personalizar!"
    )
    
    keyboard = [[InlineKeyboardButton("« Voltar", callback_data='back_main')]]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )


# ============= COMANDO /voz (TTS) =============

# Voz TTS por usuário (user_id -> voice_id)
user_tts_voices: Dict[int, str] = {}


def _get_user_tts_voice(user_id: int) -> str:
    """Retorna a voz TTS do usuário ou a padrão"""
    if TTS_AVAILABLE:
        return user_tts_voices.get(user_id, TTS_DEFAULT_VOICE)
    return ""


async def cmd_voz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu de seleção de voz TTS"""
    if not TTS_AVAILABLE or not TTS_ENABLED:
        target = update.callback_query or update
        msg = "❌ TTS não está habilitado. Configure as variáveis TTS no .env"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    user_id = update.effective_user.id
    current_voice = _get_user_tts_voice(user_id)
    current_name = tts_get_voice_name(current_voice)

    # Menu principal: botão Todos + idiomas
    keyboard = [[InlineKeyboardButton("🌐 Todas as Vozes", callback_data="tts_lang_all")]]
    row = []
    for code, label in TTS_IDIOMAS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"tts_lang_{code}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("« Voltar", callback_data='back_main')])

    text = (
        f"🎙️ *Seleção de Voz TTS*\n\n"
        f"🎤 Voz atual: *{current_name}*\n\n"
        "Escolha o idioma para ver as vozes disponíveis:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )


def _clean_text_for_tts(text: str) -> str:
    """
    Limpa texto da resposta do bot para leitura TTS.
    Remove formatação Markdown, citações, emojis, fontes, footer, etc.
    Retorna texto limpo e natural para fala.
    """
    import re as _re

    # 1. Corta tudo a partir de "📚 Fontes:" ou "Fontes:" (fim da resposta real)
    for marker in ['📚 Fontes:', '📚 *Fontes:', 'Fontes:']:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]

    # 2. Remove linhas de footer (modelo | 🔍 focus | 💬 X msg)
    lines = text.split('\n')
    clean_lines = []
    for line in lines:
        stripped = line.strip().strip('`')
        if '🔍' in stripped and '💬' in stripped and 'msg' in stripped:
            continue
        # Pula linhas que são só separadores
        if _re.match(r'^[\s\-_=─━]{3,}$', stripped):
            continue
        clean_lines.append(line)
    text = '\n'.join(clean_lines)

    # 3. Remove citações numéricas [1], [2], [1][2], etc.
    text = _re.sub(r'\[(\d+)\]', '', text)

    # 4. Remove links Markdown [texto](url) → mantém só "texto"
    text = _re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)

    # 5. Remove formatação Markdown
    text = _re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)  # ***bold italic***
    text = _re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold**
    text = _re.sub(r'\*(.+?)\*', r'\1', text)            # *italic*
    text = _re.sub(r'__(.+?)__', r'\1', text)            # __underline__
    text = _re.sub(r'_(.+?)_', r'\1', text)              # _italic_
    text = _re.sub(r'~~(.+?)~~', r'\1', text)            # ~~strikethrough~~
    text = _re.sub(r'`([^`]+)`', r'\1', text)            # `code`
    text = _re.sub(r'```[\s\S]*?```', '', text)          # ```code blocks```

    # 6. Remove headers Markdown (# ## ### etc.)
    text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)

    # 7. Remove bullet points (- * •) no início de linhas
    text = _re.sub(r'^\s*[-*•]\s+', '', text, flags=_re.MULTILINE)
    # Remove listas numeradas (1. 2. etc.)
    text = _re.sub(r'^\s*\d+\.\s+', '', text, flags=_re.MULTILINE)

    # 8. Remove emojis comuns
    text = _re.sub(
        r'[\U0001F300-\U0001F9FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F'
        r'\U0000200D\U00002600-\U000026FF\U00002700-\U000027BF'
        r'\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]+', '', text
    )

    # 9. Remove URLs soltas
    text = _re.sub(r'https?://\S+', '', text)

    # 10. Limpa espaços extras
    text = _re.sub(r'[ \t]+', ' ', text)           # múltiplos espaços → 1
    text = _re.sub(r'\n{3,}', '\n\n', text)         # múltiplas linhas vazias → 2
    text = text.strip()

    return text


async def _handle_tts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Processa callbacks de botões TTS. Retorna True se processou."""
    query = update.callback_query
    data = query.data

    if not data.startswith("tts_"):
        return False

    await query.answer()
    user_id = update.effective_user.id

    # Seleção de idioma → listar vozes
    if data.startswith("tts_lang_"):
        lang_code = data.replace("tts_lang_", "")
        current_voice = _get_user_tts_voice(user_id)

        await query.edit_message_text("🔄 Carregando vozes...", parse_mode='Markdown')

        try:
            voices = tts_fetch_voices(filtro_idioma=lang_code)
        except Exception as e:
            logger.error(f"Erro ao buscar vozes: {e}")
            await query.edit_message_text(f"❌ Erro ao buscar vozes: {e}")
            return True

        if not voices:
            await query.edit_message_text(
                f"❌ Nenhuma voz encontrada para *{lang_code}*.\n\nVerifique se o token Inworld está válido.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Voltar", callback_data='menu_voz')]
                ]),
                parse_mode='Markdown'
            )
            return True

        keyboard = []
        for v in voices[:20]:  # Limita a 20 vozes
            voice_id = v.get('voiceId') or v.get('name', '')
            display = v.get('displayName', voice_id[-15:])
            check = " ✅" if voice_id == current_voice else ""
            keyboard.append([
                InlineKeyboardButton(f"🎤 {display}{check}", callback_data=f"tts_set_{voice_id}")
            ])

        keyboard.append([InlineKeyboardButton("« Idiomas", callback_data='menu_voz')])

        lang_name = "Todas" if lang_code == "all" else TTS_IDIOMAS.get(lang_code, lang_code)
        await query.edit_message_text(
            f"🎙️ *Vozes - {lang_name}*\n\n"
            f"Encontradas {len(voices)} vozes (mostrando até 20). Toque para selecionar:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return True

    # Seleção de voz específica
    if data.startswith("tts_set_"):
        voice_id = data.replace("tts_set_", "")
        user_tts_voices[user_id] = voice_id
        voice_name = tts_get_voice_name(voice_id)

        await query.edit_message_text(
            f"✅ *Voz alterada!*\n\n"
            f"🎤 Nova voz: *{voice_name}*\n\n"
            f"O próximo áudio usará esta voz.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎙️ Mudar Voz", callback_data='menu_voz')],
                [InlineKeyboardButton("« Menu", callback_data='back_main')]
            ]),
            parse_mode='Markdown'
        )
        return True

    # Botão "Ouvir" - gera áudio da resposta sob demanda
    if data == "tts_listen":
        await query.answer("🎙️ Gerando áudio...")
        # Pega o texto da mensagem que contém o botão
        msg_text = query.message.text or ""
        if not msg_text:
            await query.answer("❌ Sem texto para gerar áudio.", show_alert=True)
            return True

        # ── Primeiro tenta extrair só o texto de RESPOSTA_SIMPLES:(((texto))) ──
        _, resposta_simples = tts_extract_simple_response(msg_text)
        if resposta_simples:
            audio_text = resposta_simples
            logger.info(f"🎙️ TTS Listen: Usando texto de RESPOSTA_SIMPLES ({len(audio_text)} chars)")
        else:
            # Fallback: limpa e usa o texto completo
            audio_text = _clean_text_for_tts(msg_text)
            logger.info(f"🎙️ TTS Listen: Usando texto limpo completo ({len(audio_text)} chars)")

        if len(audio_text) < 5:
            await query.answer("❌ Texto muito curto.", show_alert=True)
            return True

        # Trunca para TTS (máx 2000 chars)
        if len(audio_text) > 2000:
            audio_text = audio_text[:2000]

        try:
            user_voice = _get_user_tts_voice(user_id)
            logger.info(f"🎙️ TTS Listen: Gerando áudio ({len(audio_text)} chars) voz={user_voice[-15:]}")
            audio_bytes = await tts_generate_audio(audio_text, voice_id=user_voice)
            if audio_bytes:
                with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name
                with open(tmp_path, 'rb') as audio_file:
                    await query.message.reply_voice(
                        voice=audio_file,
                        caption="🎙️ Resposta em áudio"
                    )
                os.remove(tmp_path)
                logger.info("✅ TTS Listen: Áudio enviado")
            else:
                await query.message.reply_text("❌ Falha ao gerar áudio. Token TTS pode estar expirado.")
        except Exception as e:
            logger.error(f"Erro TTS Listen: {e}")
            await query.message.reply_text(f"❌ Erro ao gerar áudio: {e}")
        return True

    return False


# ============= COMANDO /limpar =============

async def cmd_limpar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Limpa histórico de conversação no servidor MCP"""
    user_id = update.effective_user.id
    
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            response = await client.post(
                f"{MCP_API}/clear",
                json={"user_id": str(user_id)}
            )
            data = response.json()
        
        msg = data.get('message', 'Histórico limpo!')
        saved_id = data.get('saved_conversation_id')
        
        response_text = f"🗑️ *{msg}*\n\n"
        if saved_id:
            response_text += f"💾 *ID Salvo:* `{saved_id}`\n"
            response_text += f"Use `/importar {saved_id}` no futuro.\n\n"
            
        response_text += "Iniciando uma nova conversa do zero."
        
        await update.message.reply_text(
            response_text,
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(
            "🗑️ *Histórico limpo!*\n\n"
            "Iniciando uma nova conversa do zero.",
            parse_mode='Markdown'
        )


# ============= COMANDO /importar =============

async def cmd_importar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Importa o contexto de uma conversa salva"""
    user_id = update.effective_user.id
    args = context.args
    
    if not args:
        await update.message.reply_text(
            "⚠️ Use: `/importar <ID>`\n"
            "Exemplo: `/importar a1b2c3d4`\n\n"
            "Use `/historico` para ver os IDs.",
            parse_mode='Markdown'
        )
        return
        
    conv_id = args[0]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        # Carrega histórico da API
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            response = await client.post(
                f"{MCP_API}/history/load",
                json={"user_id": str(user_id), "conversation_id": conv_id}
            )
            
        if response.status_code != 200:
            await update.message.reply_text("❌ Histórico não encontrado.", parse_mode='Markdown')
            return
            
        data = response.json()
        conversation = data.get('conversation', {})
        messages = conversation.get('messages', [])
        title = conversation.get('title', 'Sem título')
        
        if not messages:
            await update.message.reply_text("⚠️ Histórico vazio.", parse_mode='Markdown')
            return
            
        # Formata contexto
        context_text = f"Contexto importado da conversa '{title}' (ID: {conv_id}):\n\n"
        for msg in messages:
            role = "USUÁRIO" if msg.get('role') == 'user' else "ASSISTENTE"
            content = msg.get('content', '')
            context_text += f"[{role}]: {content}\n\n"
            
        # Envia como prompt para a IA
        user_query = (
            f"Estou fornecendo um contexto de uma conversa anterior para nossa referência.\n"
            f"Por favor, leia e confirme que entendeu o contexto. Não precisa resumir, apenas confirme.\n\n"
            f"--- INÍCIO DO CONTEXTO ---\n"
            f"{context_text[:10000]}..." # Limite de segurança
            f"\n--- FIM DO CONTEXTO ---"
        )
        
        # Envia para o MCP /search
        config = get_user_config(user_id)
        async with httpx.AsyncClient(timeout=120.0, headers=_mcp_headers()) as client:
            payload = {
                "query": user_query,
                "user_id": str(user_id),
                "model": _canonical_model_id(config['model']),
                "focus": "writing", # Focus writing é bom para processar texto
                "return_citations": False
            }
            
            response = await client.post(f"{MCP_API}/search", json=payload)
            response.raise_for_status()
            search_data = response.json()
            
        answer = search_data.get('answer', 'Contexto processado.')
        
        await update.message.reply_text(
            f"✅ *Contexto Importado!*\n"
            f"Dívida `{title}` foi adicionada ao contexto atual.\n\n"
            f"🤖 *Resposta da IA:*\n_{answer}_",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Erro ao importar: {e}")
        await update.message.reply_text("❌ Erro ao importar contexto.", parse_mode='Markdown')


# ============= COMANDO /new =============

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cria uma nova conversa (limpa a anterior)"""
    user_id = update.effective_user.id
    
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            response = await client.post(
                f"{MCP_API}/clear",
                json={"user_id": str(user_id)}
            )
            data = response.json()
        
        msg_count = 0
        saved_id = data.get('saved_conversation_id')
        
        if 'mensagens' in data.get('message', ''):
            import re
            match = re.search(r'\((\d+)', data.get('message', ''))
            if match:
                msg_count = int(match.group(1))
        
        msg_text = f"✨ *Nova conversa iniciada!*\n\n"
        if saved_id:
             msg_text += f"💾 *Histórico salvo:* `{saved_id}`\n"
             msg_text += f"Use `/importar {saved_id}` para recuperar este contexto.\n\n"
             
        msg_text += f"Conversa anterior encerrada{f' ({msg_count} mensagens)' if msg_count else ''}.\n"
        msg_text += "Me pergunte qualquer coisa! 🚀"
        
        # Envia a resposta (compatível com comando e callback)
        if update.callback_query:
            # Se veio de botão, confirma o callback e manda nova mensagem
            await update.callback_query.answer("Nova conversa iniciada!")
            await update.effective_message.reply_text(msg_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(msg_text, parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"Erro no cmd_new: {e}")
        fallback_text = "✨ *Nova conversa iniciada!*\n\nMe pergunte qualquer coisa! 🚀"
        if update.callback_query:
            await update.effective_message.reply_text(fallback_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(fallback_text, parse_mode='Markdown')


# ============= COMANDO /historico =============

async def cmd_historico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista conversas salvas"""
    user_id = update.effective_user.id
    
    # Prepara envio (suporta message e callback)
    if update.callback_query:
        await update.callback_query.answer()
        # Edita ou envia nova msg? Melhor enviar nova para histórico não sumir rápido
        # Mas para menu, editar é mais fluido. O usuário decide com "voltar".
        # Vamos editar para ficar clean.
        reply_method = update.callback_query.edit_message_text
        reply_attr = {} # edit_message não aceita reply_to_message_id
    else:
        reply_method = update.message.reply_text
        reply_attr = {}

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            response = await client.get(f"{MCP_API}/history/list", params={"user_id": str(user_id)})
            data = response.json()
            
        conversations = data.get('conversations', [])
        
        if not conversations:
            text = (
                "📂 *Seu histórico está vazio.*\n\n"
                "Use `/new` para criar novas conversas e elas serão salvas automaticamente ao limpar."
            )
            # Se for callback, pode ter botão "voltar"
            kb = [[InlineKeyboardButton("« Voltar", callback_data='back_main')]] if update.callback_query else []
            
            await reply_method(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
            return
        
        keyboard = []
        text = "📂 *Histórico de Conversas:*\n\n"
        
        for conv in conversations:
            conv_id = conv.get('id')
            title = conv.get('title', 'Sem título')
            date_str = conv.get('created_at', '')[:10]  # YYYY-MM-DD
            msg_count = conv.get('message_count', 0)
            
            # Formata data
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(conv.get('created_at'))
                date_fmt = dt.strftime("%d/%m %H:%M")
            except:
                date_fmt = date_str
            
            # Adiciona ao texto
            text += f"🔹 `{date_fmt}` - *{title}* ({msg_count} msgs)\n"
            
            # Adiciona botão
            keyboard.append([InlineKeyboardButton(
                f"📂 Abrir: {title[:20]}...",
                callback_data=f'load_history_{conv_id}'
            )])
            
        keyboard.append([InlineKeyboardButton("« Voltar", callback_data='back_main')])
        
        await reply_method(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Erro ao listar histórico: {e}")
        err_text = "❌ Erro ao buscar histórico."
        await reply_method(err_text, parse_mode='Markdown')



# ============= COMANDO /denovo (RETRY) =============

async def cmd_denovo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tenta recuperar a última resposta do backend (Retry)"""
    user_id = update.effective_user.id
    
    await update.message.reply_text("🔄 Verificando histórico no servidor...", parse_mode='Markdown')
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            response = await client.get(f"{MCP_API}/last_response", params={"user_id": user_id})
            
            if response.status_code == 200:
                data = response.json()
                answer = data.get('answer', '')
                
                if not answer:
                    await update.message.reply_text("❌ A última resposta estava vazia.")
                    return
                    
                await update.message.reply_text("✅ Resposta recuperada! Processando arquivos...")
                
                # Reutiliza a lógica robusta de extração
                final_text = await extract_and_send_files(update, answer)
                
                # Envia o texto final formatado
                await update.message.reply_text(final_text, parse_mode='Markdown', disable_web_page_preview=True)
                
            elif response.status_code == 404:
                await update.message.reply_text("❌ Nenhuma conversa recente encontrada na memória do servidor.")
            else:
                await update.message.reply_text(f"❌ Erro ao buscar: {response.status_code}")
                
    except Exception as e:
        logger.error(f"Erro cmd_denovo: {e}")
        await update.message.reply_text(f"❌ Erro ao tentar recuperar: {e}")


# ============= DETECÇÃO DE TAREFAS =============

def detect_task_proposal(text: str) -> Optional[Dict[str, Any]]:
    """
    Detecta propostas de tarefa no texto da resposta.
    Retorna dict com dados da tarefa ou None.
    """
    # Padrões comuns de proposta de tarefa
    task_patterns = [
        r"##\s*Detalhes da Tarefa",
        r"\*\*Nome\*\*:\s*(.+)",
        r"\*\*Prompt\*\*:\s*(.+)",
        r"\*\*Agendamento\*\*:\s*(.+)",
        r"Vou propor.*tarefa",
        r"criar uma tarefa.*agend",
    ]
    
    # Verifica se parece uma proposta de tarefa
    is_task = any(re.search(p, text, re.IGNORECASE) for p in task_patterns[:2])
    if not is_task:
        return None
    
    # Extrai dados
    result = {
        "name": None,
        "prompt": None,
        "schedule_type": "daily",
        "schedule_time": "09:00"
    }
    
    # Nome
    match = re.search(r"\*\*Nome\*\*:\s*(.+?)(?:\n|$)", text)
    if match:
        result["name"] = match.group(1).strip()
    
    # Prompt
    match = re.search(r"\*\*Prompt\*\*:\s*[\"']?(.+?)[\"']?(?:\n|$)", text)
    if match:
        result["prompt"] = match.group(1).strip()
    
    # Agendamento
    match = re.search(r"\*\*Agendamento\*\*:\s*(.+?)(?:\n|$)", text)
    if match:
        sched_text = match.group(1).lower()
        if "diário" in sched_text or "daily" in sched_text or "todo dia" in sched_text:
            result["schedule_type"] = "daily"
        elif "uma vez" in sched_text or "once" in sched_text:
            result["schedule_type"] = "once"
        
        # Extrai horário
        time_match = re.search(r"(\d{1,2}):(\d{2})", sched_text)
        if time_match:
            result["schedule_time"] = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        else:
            # Tenta pegar hora AM/PM
            time_match = re.search(r"(\d{1,2})\s*(AM|PM)", sched_text, re.IGNORECASE)
            if time_match:
                hour = int(time_match.group(1))
                if time_match.group(2).upper() == "PM" and hour < 12:
                    hour += 12
                result["schedule_time"] = f"{hour:02d}:00"
    
    # Valida se tem dados mínimos
    if result["name"] or result["prompt"]:
        return result
    
    return None


async def send_task_confirmation(update: Update, task: Task) -> None:
    """Envia mensagem de confirmação com botões para a tarefa proposta"""
    text = (
        f"📋 *Proposta de Tarefa*\n\n"
        f"*Nome:* {task.name}\n"
        f"*Prompt:* _{task.prompt[:100]}{'...' if len(task.prompt) > 100 else ''}_\n"
        f"*Tipo:* {task.schedule_type}\n"
        f"*Horário:* {task.schedule_time}\n\n"
        f"Confirme para ativar:"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmar", callback_data=f"task_confirm_{task.task_id}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"task_cancel_{task.task_id}")
        ],
        [
            InlineKeyboardButton("🔁 Editar Horário", callback_data=f"task_edit_{task.task_id}")
        ]
    ]
    
    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============= COMANDO /tarefas =============

async def cmd_tarefas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista tarefas ativas do usuário"""
    user_id = update.effective_user.id
    tm = get_task_manager()
    
    # Prepara envio (suporta message e callback)
    if update.callback_query:
        await update.callback_query.answer()
        reply_method = update.callback_query.edit_message_text
    else:
        reply_method = update.message.reply_text

    if not tm:
        await reply_method("❌ Gerenciador de tarefas não disponível.")
        return
    
    tasks = tm.get_tasks(user_id)
    
    # Botão voltar sempre bom
    kb_back = [[InlineKeyboardButton("🔙 Voltar", callback_data='back_main')]]
    
    if not tasks:
        text = (
            "📋 *Suas Tarefas*\n\n"
            "_Você não tem tarefas agendadas._\n\n"
            "Peça ao bot para criar uma tarefa, exemplo:\n"
            '"Crie uma tarefa para me avisar o preço do Bitcoin todo dia às 9h"'
        )
        await reply_method(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb_back))
        return
    
    text = "📋 *Suas Tarefas Ativas:*\n\n"
    keyboard = []
    
    for task in tasks:
        status = "✅" if task.enabled else "⏸️"
        text += f"{status} *{task.name}*\n"
        text += f"   ⏰ {task.schedule_type} às {task.schedule_time}\n"
        if task.last_run:
            text += f"   📅 Última exec: {task.last_run[:16]}\n"
        text += "\n"
        
        keyboard.append([
            InlineKeyboardButton(f"🗑️ {task.name[:15]}", callback_data=f"task_delete_{task.task_id}")
        ])
    
    keyboard.append([InlineKeyboardButton("🔙 Voltar", callback_data='back_main')])
    
    await reply_method(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ============= CALLBACK HANDLER DE TAREFAS =============

async def handle_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Processa callbacks relacionados a tarefas. Retorna True se processou."""
    query = update.callback_query
    data = query.data
    
    if not data.startswith("task_"):
        return False
    
    await query.answer()
    user_id = query.from_user.id
    tm = get_task_manager()
    
    if not tm:
        await query.edit_message_text("❌ Gerenciador de tarefas não disponível.")
        return True
    
    if data.startswith("task_confirm_"):
        task_id = data.replace("task_confirm_", "")
        task = tm.confirm_pending_task(task_id)
        
        if task:
            await query.edit_message_text(
                f"✅ *Tarefa Ativada!*\n\n"
                f"*{task.name}*\n"
                f"Será executada {task.schedule_type} às {task.schedule_time}.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text("❌ Tarefa não encontrada ou já confirmada.")
    
    elif data.startswith("task_cancel_"):
        task_id = data.replace("task_cancel_", "")
        tm.cancel_pending_task(task_id)
        await query.edit_message_text("❌ Tarefa cancelada.")
    
    elif data.startswith("task_edit_"):
        task_id = data.replace("task_edit_", "")
        # Mostra opções de horário
        keyboard = [
            [
                InlineKeyboardButton("06:00", callback_data=f"task_time_{task_id}_06:00"),
                InlineKeyboardButton("09:00", callback_data=f"task_time_{task_id}_09:00"),
                InlineKeyboardButton("12:00", callback_data=f"task_time_{task_id}_12:00"),
            ],
            [
                InlineKeyboardButton("15:00", callback_data=f"task_time_{task_id}_15:00"),
                InlineKeyboardButton("18:00", callback_data=f"task_time_{task_id}_18:00"),
                InlineKeyboardButton("21:00", callback_data=f"task_time_{task_id}_21:00"),
            ]
        ]
        await query.edit_message_text(
            "🕐 Escolha o horário:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data.startswith("task_time_"):
        # task_time_{task_id}_{time}
        parts = data.split("_")
        task_id = parts[2]
        new_time = parts[3]
        
        if task_id in tm.pending_tasks:
            tm.pending_tasks[task_id].schedule_time = new_time
            task = tm.confirm_pending_task(task_id)
            await query.edit_message_text(
                f"✅ *Tarefa Ativada!*\n\n"
                f"*{task.name}*\n"
                f"Será executada às *{new_time}*.",
                parse_mode='Markdown'
            )
    
    elif data.startswith("task_delete_"):
        task_id = data.replace("task_delete_", "")
        if tm.delete_task(user_id, task_id):
            await query.edit_message_text("🗑️ Tarefa removida com sucesso!")
        else:
            await query.edit_message_text("❌ Erro ao remover tarefa.")
    
    return True


# ============= COMANDO /library =============

async def cmd_library(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alterna o modo Save to Library (Nuvem)"""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            # POST faz o toggle
            response = await client.post(f"{MCP_API}/config/library")
            data = response.json()
            
            enabled = data.get('enabled', False)
            msg = data.get('message', '')
            
            # Feedback com emoji
            if enabled:
                text = f"☁️ *{msg}* - Modo Nuvem Ativo\n\n" \
                       f"Resetando contexto para iniciar uma nova conversa limpa na Library..."
            else:
                text = f"🏠 *{msg}* - Modo Local Ativo\n\n" \
                       f"Conversas salvas apenas no dispositivo."
            
            # Envia resposta
            if update.callback_query:
                await update.callback_query.answer()
                await update.effective_message.reply_text(text, parse_mode='Markdown')
            else:
                await update.message.reply_text(text, parse_mode='Markdown')

            # SE ATIVOU, OBRIGA O RESET (CMD_NEW)
            if enabled:
                # Pequeno delay visual
                await asyncio.sleep(1)
                await cmd_new(update, context)
            
    except Exception as e:
        logger.error(f"Erro library toggle: {e}")
        error_text = "❌ Erro ao alterar configuração."
        if update.callback_query:
            await update.effective_message.reply_text(error_text)
        else:
            await update.message.reply_text(error_text)


# ============= COMANDO /vpn =============

async def cmd_vpn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Controle da VPN: status, ativar/desativar, reconectar"""
    
    # Suporte a callback
    if update.callback_query:
        await update.callback_query.answer()
        reply_method = update.callback_query.edit_message_text
        msg = update.effective_message
    else:
        msg = await update.message.reply_text("🔄 *Verificando VPN...*", parse_mode='Markdown')
        reply_method = msg.edit_text

    # Verifica se VPN está habilitada
    if not VPN_ENABLED:
        text = (
            "🔐 *Controle VPN*\n\n"
            "⚠️ VPN não está habilitada neste ambiente.\n\n"
            "Para ativar, configure no `.env`:\n"
            "`VPN_ENABLED=true`\n"
            "`OPENVPN_USER=...`\n"
            "`OPENVPN_PASSWORD=...`"
        )
        buttons = [[InlineKeyboardButton("🔙 Voltar", callback_data="back_main")]]
        await reply_method(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
        return

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            # Obtém status atual
            try:
                status_resp = await client.get(f"{VPN_API}/v1/openvpn/status")
                status_data = status_resp.json()
                vpn_status = status_data.get('status', 'unknown')
            except:
                vpn_status = 'offline'
            
            # Obtém IP público
            try:
                ip_resp = await client.get(f"{VPN_API}/v1/publicip/ip")
                ip_data = ip_resp.json()
                public_ip = ip_data.get('public_ip', 'Desconhecido')
            except:
                public_ip = 'Erro ao obter'
            
            # Emoji de status
            if vpn_status == 'running':
                status_emoji = "🟢"
                status_text = "Conectada"
            elif vpn_status == 'stopped':
                status_emoji = "🔴"
                status_text = "Desconectada"
            else:
                status_emoji = "⚪"
                status_text = vpn_status.capitalize()
        
        # Monta mensagem
        text = (
            f"🔐 *Controle VPN* (VPS)\n\n"
            f"{status_emoji} *Status:* {status_text}\n"
            f"🌐 *IP Público:* `{public_ip}`\n\n"
            f"Selecione uma ação:"
        )
        
        # Botões
        buttons = []
        if vpn_status == 'running':
            buttons.append([
                InlineKeyboardButton("🔄 Novo IP", callback_data="vpn_reconnect"),
                InlineKeyboardButton("🔴 Desativar", callback_data="vpn_stop")
            ])
        else:
            buttons.append([
                InlineKeyboardButton("🟢 Ativar", callback_data="vpn_start")
            ])
        
        buttons.append([InlineKeyboardButton("🔙 Voltar", callback_data="back_main")])
        
        await reply_method(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
        
    except Exception as e:
        logger.error(f"Erro cmd_vpn: {e}")
        await reply_method(f"❌ Erro ao verificar VPN: {e}", parse_mode='Markdown')


async def handle_vpn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handler para callbacks de VPN"""
    query = update.callback_query
    data = query.data
    
    if not data.startswith("vpn_"):
        return False
    
    await query.answer()
    
    if not VPN_ENABLED:
        await query.edit_message_text("⚠️ VPN não habilitada neste ambiente.", parse_mode='Markdown')
        return True
    
    action = data.replace("vpn_", "")
    
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_mcp_headers()) as client:
            if action == "start":
                await query.edit_message_text("🔌 *Ativando VPN...*", parse_mode='Markdown')
                await client.put(f"{VPN_API}/v1/openvpn/status", json={"status": "running"})
                await asyncio.sleep(3)  # Aguarda conexão
                
            elif action == "stop":
                await query.edit_message_text("🔌 *Desativando VPN...*", parse_mode='Markdown')
                await client.put(f"{VPN_API}/v1/openvpn/status", json={"status": "stopped"})
                await asyncio.sleep(1)
                
            elif action == "reconnect":
                await query.edit_message_text("🔄 *Reconectando VPN (novo IP)...*", parse_mode='Markdown')
                # Stop then start para forçar novo servidor
                await client.put(f"{VPN_API}/v1/openvpn/status", json={"status": "stopped"})
                await asyncio.sleep(2)
                await client.put(f"{VPN_API}/v1/openvpn/status", json={"status": "running"})
                await asyncio.sleep(5)  # Aguarda reconexão
        
        # Atualiza status após ação
        await cmd_vpn(update, context)
        
    except Exception as e:
        logger.error(f"Erro VPN action {action}: {e}")
        await query.edit_message_text(f"❌ Erro: {e}", parse_mode='Markdown')
    
    return True


# ============= COMANDO /token =============
async def cmd_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gerenciador de Tokens (Dashboard)"""
    
    # Suporte a callback
    if update.callback_query:
        await update.callback_query.answer()
        reply_method = update.callback_query.edit_message_text
    else:
        msg = await update.message.reply_text("🔄 *Carregando painel de tokens...*", parse_mode='Markdown')
        reply_method = msg.edit_text

    # Se usuário passou argumento: /token <sess> (Modo Manual)
    if context.args:
        token = context.args[0]
        try:
            await update.message.delete()
        except: pass
        
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                resp = await client.post(f"{MCP_API}/config/token", json={"token": token})
                if resp.status_code == 200:
                    await reply_method("✅ *Token Inserido Manualmente!*", parse_mode='Markdown')
                else:
                    await reply_method(f"❌ Erro: {resp.text}", parse_mode='Markdown')
        except Exception as e:
            await reply_method(f"❌ Erro de conexão: {e}", parse_mode='Markdown')
        return

    # Modo Dashboard
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
            status_resp = await client.get(f"{MCP_API}/tokens/status")
            try:
                status = status_resp.json()
            except:
                status = {}
            
        current = status.get('current_account', {})
        total = status.get('total_accounts', 0)
        idx = status.get('current_index', 0) + 1
        is_active = status.get('active', False)
        
        status_emoji = "🟢" if is_active else "🔴"
        
        # Formata data
        last_val = current.get('last_validated')
        if last_val and isinstance(last_val, str):
            try:
                dt = datetime.fromisoformat(last_val)
                last_val = dt.strftime("%d/%m/%Y %H:%M")
            except: pass
        else:
            last_val = "Nunca"
        
        text = (
            f"🔑 *Gestão de Tokens* {status_emoji}\n\n"
            f"👤 *Conta:* `{current.get('email', 'N/A')}`\n"
            f"🏷️ *Nome:* `{current.get('name', 'N/A')}`\n"
            f"🔢 *Índice:* {idx}/{total}\n"
            f"📅 *Última Validação:* `{last_val}`\n\n"
            f"Selecione uma ação:"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Validar", callback_data='token_validate'),
                InlineKeyboardButton("🔄 Rotação (Next)", callback_data='token_rotate')
            ],
            [
                InlineKeyboardButton("🚀 Smart Refresh (Cookies)", callback_data='token_refresh')
            ],
            [
                InlineKeyboardButton("🆕 Novo Refresh OTP", callback_data='token_new_refresh')
            ],
            [
                InlineKeyboardButton("📊 Pool Status", callback_data='pool_status'),
                InlineKeyboardButton("🚀 Refresh All", callback_data='pool_smart_refresh')
            ],
            [
                InlineKeyboardButton("🩺 Validar Pool", callback_data='pool_validate_all'),
                InlineKeyboardButton("🗑️ Limpar Inválidos", callback_data='pool_clear_invalid')
            ],
            [InlineKeyboardButton("🔙 Voltar", callback_data='back_main')]
        ]
        
        await reply_method(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        
    except Exception as e:
        logger.error(f"Erro cmd_token: {e}")
        await reply_method(f"❌ Erro ao carregar painel: {e}", parse_mode='Markdown')


# ============= COMANDO /teste =============

async def cmd_teste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Executa diagnóstico do sistema"""
    try:
        msg = await update.message.reply_text("🕵️ *Executando diagnóstico...*", parse_mode='Markdown')
        
        async with httpx.AsyncClient(timeout=15.0, headers=_mcp_headers()) as client:
            try:
                # Chama endpoint de diagnóstico
                response = await client.get(f"{MCP_API}/diagnostics")
                data = response.json()
                
                status_mcp = "✅ Online" if data.get('mcp_status') == 'online' else "❌ Offline"
                ip = data.get('public_ip', 'Desconhecido')
                auth_status = data.get('perplexity_auth', 'unknown')
                
                auth_emoji = "✅ Válido" if auth_status == 'configured' else "❌ Inválido/Ausente"
                if auth_status == 'missing_token': auth_emoji = "⚠️ Sem Token"
                
                report = (
                    "🕵️ *Relatório de Diagnóstico*\n\n"
                    f"🤖 *MCP Server:* {status_mcp}\n"
                    f"🌐 *IP de Saída:* `{ip}`\n"
                    f"🔑 *Perplexity:* {auth_emoji}\n\n"
                )
                
                if data.get('auth_error'):
                    report += f"⚠️ *Erro Auth:* `{data['auth_error']}`\n"
                    
                await msg.edit_text(report, parse_mode='Markdown')
                
            except httpx.ConnectError:
                await msg.edit_text("❌ *MCP Offline*: Não consegui conectar ao servidor.", parse_mode='Markdown')
                
    except Exception as e:
        logger.error(f"Erro no teste: {e}")
        await update.message.reply_text("❌ Erro ao executar teste.", parse_mode='Markdown')


# ============= HANDLERS DE CALLBACK =============

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler para todos os botões inline"""
    query = update.callback_query
    data = query.data
    
    # Processa callbacks de VPN primeiro
    if data.startswith("vpn_"):
        await handle_vpn_callback(update, context)
        return
    
    # Processa callbacks de tarefas
    if data.startswith("task_"):
        await handle_task_callback(update, context)
        return
    
    # Processa callbacks de TTS/Voz
    if data.startswith("tts_"):
        if TTS_AVAILABLE:
            await _handle_tts_callback(update, context)
        return
    
    await query.answer()
    
    user_id = update.effective_user.id
    
    # Navegação e Menus Principais
    if data == 'back_main':
        await start(update, context)
        return
    
    # Handlers do Menu Principal
    if data == 'menu_new':
        await cmd_new(update, context)
    elif data == 'menu_history':
        await cmd_historico(update, context)
    elif data == 'menu_tokens':
        await cmd_token(update, context)
    elif data == 'menu_tasks':
        await cmd_tarefas(update, context)
    elif data == 'menu_library':
        await cmd_library(update, context)
    elif data == 'menu_vpn':
        await cmd_vpn(update, context)
        
    # Sub-menus
    elif data == 'menu_modelos':
        await cmd_modelos(update, context)
    elif data == 'menu_busca':
        await cmd_busca(update, context)
    elif data == 'menu_normal':
        await cmd_normal(update, context)
    elif data == 'menu_config':
        await cmd_config(update, context)
    elif data == 'menu_ajuda':
        await cmd_ajuda(update, context)
    elif data == 'menu_tempo':
        await cmd_tempo(update, context)
    elif data == 'menu_voz':
        await cmd_voz(update, context)
    
    # Seleção de modelo
    elif data.startswith('modelsel_') or data.startswith('set_model_'):
        if data.startswith('modelsel_'):
            model_key = data.replace('modelsel_', '')
            model = _model_selection_cache.get(user_id, {}).get(model_key)
            if not model:
                models = await get_available_models()
                try:
                    model = models[int(model_key)]["id"]
                except Exception:
                    model = "perplexity/best"
        else:
            model = data.replace('set_model_', '')
        model = _canonical_model_id(model)
        config = get_user_config(user_id)
        config['model'] = model
        config['mode'] = 'busca'
        save_user_config(user_id, config)
        
        await query.answer(f"✅ Modelo selecionado: {model}")
        await cmd_modelos(update, context)
    
    elif data.startswith('load_history_'):
        conv_id = data.replace('load_history_', '')
        
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                response = await client.post(
                    f"{MCP_API}/history/load",
                    json={"user_id": str(user_id), "conversation_id": conv_id}
                )
                data = response.json()
            
            if response.status_code == 200:
                title = data.get('conversation', {}).get('title', 'Conversa')
                msg_count = len(data.get('conversation', {}).get('messages', []))
                
                await query.answer("✅ Conversa carregada!")
                await query.edit_message_text(
                    f"📂 *Conversa Restaurada!* \n\n"
                    f"📝 *{title}*\n"
                    f"💬 *{msg_count} mensagens recuperadas*\n\n"
                    f"Envie uma mensagem para continuar desta conversa.",
                    parse_mode='Markdown'
                )
            else:
                await query.answer("❌ Erro ao carregar.")
                
        except Exception as e:
            logger.error(f"Erro ao carregar conversa: {e}")
            await query.answer("❌ Erro de conexão.")

    # Seleção de focus
    elif data.startswith('set_focus_'):
        focus = data.replace('set_focus_', '')
        config = get_user_config(user_id)
        config['focus'] = focus
        config['mode'] = 'busca'
        save_user_config(user_id, config)
        
        await query.answer(f"✅ Focus {focus.upper()} selecionado!")
        await cmd_busca(update, context)

    # Seleção de tempo
    elif data.startswith('set_time_'):
        time_range = data.replace('set_time_', '')
        config = get_user_config(user_id)
        config['time_range'] = time_range
        save_user_config(user_id, config)
        
        await query.answer(f"✅ Tempo {time_range.upper()} selecionado!")
        await cmd_tempo(update, context)
    elif data == 'toggle_reasoning':
        config = get_user_config(user_id)
        config['reasoning'] = not config['reasoning']
        save_user_config(user_id, config)
        
        status = "ativado" if config['reasoning'] else "desativado"
        await query.answer(f"Reasoning {status}!")
        await cmd_config(update, context)
    
    elif data == 'toggle_citations':
        config = get_user_config(user_id)
        config['return_citations'] = not config['return_citations']
        save_user_config(user_id, config)
        
        status = "ativadas" if config['return_citations'] else "desativadas"
        await query.answer(f"Citações {status}!")
        await cmd_config(update, context)
    
    elif data == 'toggle_images':
        config = get_user_config(user_id)
        config['return_images'] = not config['return_images']
        save_user_config(user_id, config)
        
        status = "ativadas" if config['return_images'] else "desativadas"
        await query.answer(f"Imagens {status}!")
        await cmd_config(update, context)


# ============= HANDLER DE MENSAGENS DE TEXTO =============

async def reply_chunked(update: Update, text: str):
    """Envia mensagem longa dividida em partes para evitar erro 400 do Telegram"""
    MAX_LENGTH = 4000
    
    if len(text) <= MAX_LENGTH:
        try:
            await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
        except Exception:
             # Fallback: Se der erro de markdown (comum com caracteres especiais), tenta raw
             await update.message.reply_text(text, disable_web_page_preview=True)
        return

    # Divide em partes
    parts = [text[i:i+MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
    
    for i, part in enumerate(parts):
        try:
            # Tenta mandar com markdown
            await update.message.reply_text(part, parse_mode='Markdown', disable_web_page_preview=True)
        except Exception:
            # Se falhar (ex: corte no meio de um bloco de código), manda raw
            await update.message.reply_text(part)


async def extract_and_send_files(update: Update, text: str) -> str:
    """
    Extrai blocos de código (>50 chars), envia como arquivos e remove do texto original.
    Retorna o texto limpo.
    """
    import re
    import tempfile
    
    # Regex para capturar blocos ```lang ... ```
    pattern = r"```(\w+)?\n(.*?)```"
    matches = list(re.finditer(pattern, text, re.DOTALL))
    
    file_count = 0
    clean_text = text
    
    # Mapeamento de extensões
    EXT_MAP = {
        'html': '.html', 'htm': '.html',
        'css': '.css',
        'js': '.js', 'javascript': '.js', 'typescript': '.ts', 'ts': '.ts',
        'py': '.py', 'python': '.py',
        'java': '.java',
        'c': '.c', 'cpp': '.cpp',
        'cs': '.cs', 'csharp': '.cs',
        'php': '.php',
        'sql': '.sql',
        'json': '.json',
        'xml': '.xml',
        'yaml': '.yaml', 'yml': '.yaml',
        'md': '.md',
        'sh': '.sh', 'bash': '.sh', 'shell': '.sh',
        'txt': '.txt',
        'dockerfile': 'Dockerfile'
    }

    for match in matches:
        lang = (match.group(1) or 'txt').lower()
        content = match.group(2)
        full_block = match.group(0)
        
        # Conta linhas do bloco
        line_count = content.count('\n') + 1
        
        # Ignora blocos curtos (<= 50 linhas) - mantém inline no chat
        if line_count <= 50:
            continue
            
        ext = EXT_MAP.get(lang, '.txt')
        file_count += 1
        
        # Nome inteligente
        filename = f"code_{file_count}{ext}"
        if ext == 'Dockerfile': filename = 'Dockerfile'
        
        try:
            # Cria arquivo temporário
            with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False, encoding='utf-8') as tmp:
                tmp.write(content)
                tmp_path = tmp.name
                
            # Envia arquivo
            await update.message.reply_document(
                document=open(tmp_path, 'rb'),
                filename=filename,
                caption=f"📝 Código extraído ({lang})"
            )
            
            # Limpa temp
            os.remove(tmp_path)
            
            # Remove do texto final (substitui por placeholder discreto)
            clean_text = clean_text.replace(full_block, f"\n📂 *[Arquivo enviado: {filename}]*\n")
            
        except Exception as e:
            logger.error(f"Erro code-to-file: {e}")
            
    return clean_text


async def stream_search_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: dict):
    """
    Realiza busca via streaming e atualiza mensagem no Telegram em tempo real.
    """
    user_id = payload.get('user_id')
    config = get_user_config(int(user_id))
    
    # Mensagem inicial (placeholder)
    msg = await update.message.reply_text("🧠 _Pensando..._", parse_mode='Markdown')
    
    full_answer = ""
    thinking_buffer = ""
    citations = []
    canvas_files = []  # Arquivos canvas/files gerados pelo Perplexity
    status_text = "Iniciando..."
    
    last_update_time = 0
    import time
    
    try:
        async with httpx.AsyncClient(timeout=180.0, headers=_mcp_headers()) as client:
            async with client.stream("POST", f"{MCP_API}/search_stream", json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    await msg.edit_text(f"❌ Erro no stream: {error_text.decode()[:200]}")
                    return

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                        
                    try:
                        data = json.loads(line.replace("data: ", ""))
                        
                        # Atualiza estado local
                        if "status" in data:
                            status_text = data['status']
                        
                        if "thinking" in data:
                            thinking_buffer = data['thinking']
                            status_text = "Raciocinando..."
                            
                        if "citation" in data:
                            citations.append(data['citation'])
                            status_text = f"Encontradas {len(citations)} fontes..."
                            
                        if "chunk" in data:
                            full_answer += data['chunk']
                            status_text = "Escrevendo..."

                        if "clarifying_question" in data:
                            status_text = "❓ Aguardando resposta..."
                            full_answer += "\n\n❓ *Pergunta de Esclarecimento*: Por favor, responda abaixo para continuar."

                        # Captura arquivos canvas/files gerados
                        if "file" in data:
                            canvas_files.append(data['file'])
                            fname = data['file'].get('filename', '?')
                            status_text = f"📂 Arquivo gerado: {fname}"
                            logger.info(f"📂 Canvas file recebido: {fname}")
                            
                        # Lógica de atualização da UI (Throttling ~1.5s)
                        current_time = time.time()
                        if current_time - last_update_time > 1.5 or "done" in data:
                            # Monta o texto visual
                            display_text = ""
                            
                            # 1. Bloco de Pensamento (Collapsible ou Quote)
                            if thinking_buffer:
                                # Mostra apenas as últimas linhas para não poluir, ou tudo em quote
                                # Vamos mostrar um resumo
                                th_preview = thinking_buffer[-200:].replace("\n", " ")
                                display_text += f"🧠 _{th_preview}._\n\n"
                            
                            # 2. Status e Fontes
                            if not full_answer:
                                display_text += f"🔄 *{status_text}*\n"
                                if citations:
                                    display_text += f"📚 _{len(citations)} fontes lidas_\n"
                            
                            # 3. Resposta Real
                            if full_answer:
                                display_text += full_answer
                            
                            # Adiciona cursor piscando se não acabou
                            if "done" not in data:
                                display_text += " 🟢"
                            
                            # Tenta editar (com tratamento de erro de markdown)
                            try:
                                # Limite do Telegram
                                if len(display_text) > 4000:
                                    display_text = display_text[:4000] + "..."
                                    
                                await msg.edit_text(display_text, parse_mode='Markdown')
                            except Exception:
                                # Fallback para raw em caso de erro de parse
                                try:
                                    await msg.edit_text(display_text)
                                except:
                                    pass
                                    
                            last_update_time = current_time
                            
                        if "done" in data:
                            # Garante que usamos a resposta completa e oficial do backend
                            if 'answer' in data:
                                full_answer = data['answer']
                            break
                            
                    except json.JSONDecodeError:
                        continue

        # Formatação Final Bonita
        final_text = ""
        
        # Opcional: Incluir raciocínio expandido se configurado
        if config['reasoning'] and thinking_buffer:
             final_text += f"🧠 *Raciocínio:*\n_{thinking_buffer}_\n\n---\n\n"
        
        final_text += full_answer
        
        if config['return_citations'] and citations:
            final_text += "\n\n📚 *Fontes:*\n"
            for i, cite in enumerate(citations[:5], 1):
                title = cite.get('title', 'Link')
                url = cite.get('url', '')
                final_text += f"{i}. [{title}]({url})\n"
        
        # 🔗 Footer Informativo (Restaurado)
        model_name = config.get('model', 'best')
        focus_name = config.get('focus', 'web')
        msg_count = data.get('conversation_info', {}).get('message_count', '?')
        footer = f"\n` {model_name} | 🔍 {focus_name} | 💬 {msg_count} msg `"
        final_text += footer
        
        # Só edita se for diferente do que já está (remove o cursor verde)
        tts_audio = None
        try:
            # 0. Envia arquivos Canvas gerados pelo Perplexity (PRIORITÁRIO)
            if canvas_files:
                logger.info(f"📂 Enviando {len(canvas_files)} arquivo(s) canvas ao usuário...")
                for cf in canvas_files:
                    try:
                        filename = cf.get('filename', 'arquivo.txt')
                        content = cf.get('content', '')
                        lang = cf.get('language', '')
                        if content:
                            with tempfile.NamedTemporaryFile(mode='w', suffix='', delete=False, encoding='utf-8') as tmp:
                                tmp.write(content)
                                tmp_path = tmp.name
                            with open(tmp_path, 'rb') as f:
                                await update.message.reply_document(
                                    document=f,
                                    filename=filename,
                                    caption=f"📂 Arquivo gerado ({lang})" if lang else "📂 Arquivo gerado"
                                )
                            os.remove(tmp_path)
                            logger.info(f"✅ Canvas file enviado: {filename}")
                    except Exception as e:
                        logger.error(f"❌ Erro ao enviar canvas file '{cf.get('filename','?')}': {e}")

            # 1. Tenta extrair e enviar arquivos de blocos de código (isso é PRIORITÁRIO)
            clean_text = await extract_and_send_files(update, final_text)
            
            # 2. TTS: Detecta RESPOSTASIMPLES:(((texto))) e gera áudio automaticamente
            if TTS_AVAILABLE and TTS_ENABLED:
                try:
                    user_voice = _get_user_tts_voice(int(user_id)) if TTS_AVAILABLE else None
                    clean_text, tts_audio = await tts_extract_and_generate(clean_text, voice_id=user_voice)
                except Exception as tts_err:
                    logger.error(f"Erro TTS extração: {tts_err}")
            
            # 3. Monta botão TTS se disponível
            reply_markup = None
            if TTS_AVAILABLE and TTS_ENABLED:
                reply_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎙️ Ouvir", callback_data="tts_listen")]
                ])

            # 4. Atualiza a mensagem final (tenta Markdown, fallback sem formatação)
            try:
                await msg.edit_text(clean_text, parse_mode='Markdown', disable_web_page_preview=True, reply_markup=reply_markup)
            except Exception:
                # Fallback: envia sem Markdown se parse falhar
                try:
                    await msg.edit_text(clean_text, disable_web_page_preview=True, reply_markup=reply_markup)
                except Exception as e2:
                    logger.error(f"Erro ao editar msg final: {e2}")
            
        except Exception as e:
            logger.error(f"Erro ao finalizar msg: {e}")
            try:
                await msg.edit_text(final_text, disable_web_page_preview=True)
            except Exception as e2:
                logger.error(f"Erro fatal ao editar msg final: {e2}")
        
        # 4. Envia áudio TTS SEMPRE (fora do try principal para não ser bloqueado por erros de Markdown)
        if tts_audio:
            try:
                with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
                    tmp.write(tts_audio)
                    tmp_path = tmp.name
                with open(tmp_path, 'rb') as audio_file:
                    await update.message.reply_voice(
                        voice=audio_file,
                        caption="🎙️ Resposta em áudio"
                    )
                os.remove(tmp_path)
                logger.info("✅ TTS: Áudio enviado ao usuário")
            except Exception as audio_err:
                logger.error(f"Erro ao enviar áudio TTS: {audio_err}")

    except Exception as e:
        logger.error(f"Erro stream handler: {e}")
        await msg.edit_text(f"❌ Erro: {e}")


# ============= COMANDO /local =============

async def cmd_local(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle de localização: liga/desliga busca local"""
    user_id = update.effective_user.id
    config = get_user_config(user_id)
    
    has_location = config.get('lat') is not None and config.get('lon') is not None
    
    if has_location:
        # Remove coords
        config.pop('lat', None)
        config.pop('lon', None)
        save_user_config(user_id, config)
        await update.message.reply_text(
            "📍 *Localização DESATIVADA*\n\n"
            "Suas buscas agora serão globais.\n"
            "Para reativar, envie sua localização pelo 📎 clip.",
            parse_mode='Markdown'
        )
    else:
        # Pede para enviar
        await update.message.reply_text(
            "📍 *Localização não configurada*\n\n"
            "Para ativar buscas locais:\n"
            "1. Clique no 📎 (clip) no Telegram\n"
            "2. Selecione *Localização*\n"
            "3. Envie sua localização atual\n\n"
            "Depois disso, `/local` para desligar.",
            parse_mode='Markdown'
        )


# ============= HANDLER DE LOCALIZAÇÃO =============

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Armazena localização do usuário para buscas locais"""
    user_id = update.effective_user.id
    location = update.message.location
    
    if location:
        config = get_user_config(user_id)
        config['lat'] = location.latitude
        config['lon'] = location.longitude
        save_user_config(user_id, config)
        
        await update.message.reply_text(
            f"📍 *Localização Definida!*\n\n"
            f"Lat: `{location.latitude:.4f}`\n"
            f"Lon: `{location.longitude:.4f}`\n\n"
            f"Próximas buscas usarão esta localização. Para limpar, use /config > Limpar Localização (se houver) ou apenas reinicie.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Erro ao ler localização.")


async def token_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler para ações de token"""
    query = update.callback_query
    data = query.data
    
    if data == 'token_validate':
        await query.answer("Validando token...")
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                response = await client.post(f"{MCP_API}/tokens/validate")
                result = response.json()
            
            is_valid = result.get('valid', False)
            msg = "✅ Token Válido!" if is_valid else "❌ Token Inválido/Expirado!"
            account = result.get('account', {}).get('name', 'N/A')
            
            await query.edit_message_text(
                f"{msg}\n\nConta: `{account}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Voltar", callback_data='token_menu')]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"Erro: {e}")

    elif data == 'token_rotate':
        await query.answer("Rotacionando...")
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                response = await client.post(f"{MCP_API}/tokens/rotate")
                result = response.json()
            
            if response.status_code == 200:
                msg = result.get('message', 'Rotação concluída')
                await query.answer(f"✅ {msg}")
                
                # Recarrega menu (trata "Message is not modified")
                try:
                    await cmd_token(update, context)
                except Exception:
                    pass  # Menu já está atualizado
            else:
                await query.answer(f"⚠️ {result.get('error', 'Erro')}")
        except Exception as e:
            try:
                await query.edit_message_text(f"Erro: {e}")
            except Exception:
                pass

    elif data == 'token_refresh':
        await query.answer("Iniciando Smart Refresh...")
        # Mensagem temporária de processamento
        await query.edit_message_text(
            "⏳ *Processando Smart Refresh...*\n\n"
            "Simulando navegador para extrair cookies atualizados do servidor...",
            parse_mode='Markdown'
        )
        
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=_mcp_headers()) as client:
                response = await client.get(f"{MCP_API}/tokens/refresh")
                result = response.json()
            
            if result.get('status') == 'success':
                new_preview = result.get('new_token_preview', 'N/A')
                msg = result.get('message', 'Token renovado')
                text = (
                    f"✅ *Token Renovado com Sucesso!*\n\n"
                    f"📝 *Status:* `{msg}`\n"
                    f"🔑 *Preview:* `{new_preview}`\n\n"
                    f"O bot já está usando o novo token!"
                )
            else:
                err_msg = result.get('message', 'Erro desconhecido')
                text = (
                    f"❌ *Erro no Refresh*\n\n"
                    f"📝 *Mensagem:* `{err_msg}`\n\n"
                    f"Tente exportar os cookies do navegador novamente se o erro persistir."
                )
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Voltar", callback_data='token_menu')]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Erro no callback token_refresh: {e}")
            await query.edit_message_text(f"❌ Erro de conexão com o MCP: {e}")

    elif data == 'token_new_refresh':
        # Inicia fluxo de refresh OTP via Telegram
        await query.edit_message_text(
            "📧 *Novo Refresh Token*\n\n"
            "Envie seu email do Perplexity para iniciar.\n"
            "Ex: `user@email.com`\n\n"
            "_Digite /cancelar para abortar._",
            parse_mode='Markdown'
        )
        context.user_data['waiting_for_email'] = True

    elif data == 'token_menu':
        await cmd_token(update, context)

    # ============= POOL CALLBACKS =============

    elif data == 'pool_status':
        await query.answer("Carregando pool...")
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                response = await client.get(f"{MCP_API}/tokens/pool/status")
                result = response.json()
            
            total = result.get('total', 0)
            valid = result.get('valid', 0)
            invalid = result.get('invalid', 0)
            unknown = result.get('unknown', 0)
            
            tokens_list = ""
            for t in result.get('tokens', [])[:10]:  # max 10
                status_icon = {'valid': '🟢', 'invalid': '🔴', 'unknown': '⚪'}.get(t['status'], '⚪')
                safe_name = t['name'].replace('_', '\\_')
                if t.get('email'):
                    email = t['email'].replace('_', '\\_')
                    display_name = f"{safe_name} ({email})"
                else:
                    display_name = safe_name
                
                tokens_list += f"  {status_icon} `{t['id']}` {display_name} — `{t['preview']}`\n"
            
            if not tokens_list:
                tokens_list = "  Pool vazio\n"
            
            text = (
                f"📊 *Pool de Tokens*\n\n"
                f"📦 Total: *{total}*\n"
                f"🟢 Válidos: *{valid}*\n"
                f"🔴 Inválidos: *{invalid}*\n"
                f"⚪ Desconhecidos: *{unknown}*\n\n"
                f"🔑 *Tokens:*\n{tokens_list}"
            )
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Voltar", callback_data='token_menu')]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Erro: {e}")

    elif data == 'pool_smart_refresh':
        await query.answer("Iniciando Smart Refresh All...")
        await query.edit_message_text(
            "⏳ *Smart Refresh em TODOS os tokens...*\n\n"
            "Isso pode demorar alguns segundos.",
            parse_mode='Markdown'
        )
        
        try:
            async with httpx.AsyncClient(timeout=60.0, headers=_mcp_headers()) as client:
                response = await client.post(f"{MCP_API}/tokens/pool/smart_refresh")
                result = response.json()
            
            new = result.get('new_tokens', 0)
            still = result.get('still_valid', 0)
            invalid = result.get('now_invalid', 0)
            details = result.get('details', {})
            
            new_list = ""
            for t in details.get('new_tokens', []):
                safe_source = t['source'].replace('_', '\\_')
                new_list += f"  🆕 `{t['new_id']}` ← {safe_source} — `{t['preview']}`\n"
            
            text = (
                f"🚀 *Smart Refresh Concluído!*\n\n"
                f"🆕 Novos tokens: *{new}*\n"
                f"🟢 Ainda válidos: *{still}*\n"
                f"🔴 Agora inválidos: *{invalid}*\n"
            )
            if new_list:
                text += f"\n📋 *Tokens gerados:*\n{new_list}"
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Voltar", callback_data='token_menu')]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Erro: {e}")

    elif data == 'pool_validate_all':
        await query.answer("Validando todos os tokens...")
        await query.edit_message_text(
            "⏳ *Validando todos os tokens do pool...*",
            parse_mode='Markdown'
        )
        
        try:
            async with httpx.AsyncClient(timeout=60.0, headers=_mcp_headers()) as client:
                response = await client.post(f"{MCP_API}/tokens/pool/validate_all")
                result = response.json()
            
            valid = result.get('valid', 0)
            invalid = result.get('invalid', 0)
            
            text = (
                f"🩺 *Validação do Pool Concluída!*\n\n"
                f"🟢 Válidos: *{valid}*\n"
                f"🔴 Inválidos: *{invalid}*\n"
            )
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Voltar", callback_data='token_menu')]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Erro: {e}")

    elif data == 'pool_clear_invalid':
        await query.answer("Limpando inválidos...")
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                response = await client.post(f"{MCP_API}/tokens/pool/clear_invalid")
                result = response.json()
            
            removed = result.get('removed', 0)
            remaining = result.get('remaining', 0)
            
            text = (
                f"🗑️ *Limpeza do Pool*\n\n"
                f"❌ Removidos: *{removed}*\n"
                f"📦 Restantes: *{remaining}*\n"
            )
            
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Voltar", callback_data='token_menu')]]),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Erro: {e}")

# ============= COMANDOS DE TOKEN VIA BOT =============

# ============= COMANDOS DE TOKEN VIA BOT =============

async def cmd_apagartokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """SECRET: Apaga TODOS os tokens do sistema e reseta configurações."""
    if not _check_access(update.effective_user.id):
        return

    msg = await update.message.reply_text("⚠️ *ATENÇÃO: APAGANDO TODOS OS TOKENS...*", parse_mode='Markdown')
    
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_mcp_headers()) as client:
            resp = await client.post(f"{MCP_API}/tokens/clear_all")
            result = resp.json()
        
        if result.get("status") == "success":
            deleted = result.get("details", {}).get("files_deleted", [])
            await msg.edit_text(
                f"🗑️ *SISTEMA ZERADO!*\n\n"
                f"Arquivos removidos:\n" + "\n".join([f"- `{f}`" for f in deleted]) + "\n\n"
                f"Todos os tokens foram apagados. O bot está sem acesso agora.\n"
                f"Use `/tokencolar` ou `/refresh` para adicionar novos.",
                parse_mode='Markdown'
            )
        else:
            await msg.edit_text(f"❌ Erro ao apagar: {result.get('error')}")
            
    except Exception as e:
        await msg.edit_text(f"❌ Erro de conexão: {e}")


async def _process_token(msg, token: str):
    """Lógica central: merge no browser_cookies.json + refresh. Reutilizada por cmd_tokencolar e message_handler."""
    # Validação Básica de Formato JWT
    is_jwt = token.startswith('eyJ') and len(token) > 100 and token.count('.') >= 2
    
    if not is_jwt:
        # Tenta ser flexível mas rejeita emails
        if '@' in token and '.' in token and len(token) < 50:
            await msg.edit_text("❌ Isso parece um email, não um token. O token começa com `eyJ...`")
            return
            
        if len(token) < 80:
            await msg.edit_text("❌ Token muito curto ou inválido. Certifique-se de copiar todo o valor de `__Secure-next-auth.session-token`.")
            return
    
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_mcp_headers()) as client:
            resp = await client.post(
                f"{MCP_API}/tokens/set",
                json={"token": token}
            )
            result = resp.json()
        
        if resp.status_code == 200 and result.get('status') == 'success':
            rotated = result.get('rotated', False)
            emoji = '🔄' if rotated else '✅'
            pool_info = f"📊 Pool: {result.get('pool_valid', '?')}/{result.get('pool_total', '?')} válidos\n" if result.get('pool_total') else ""
            await msg.edit_text(
                f"{emoji} *{result.get('message', 'Token ativado!')}*\n\n"
                f"🔑 Preview: `{result.get('token_preview', '****')}`\n"
                f"🍪 Cookies mesclados: {result.get('cookies_count', '?')}\n"
                f"🔄 Rotação: {'✅ Novo token gerado!' if rotated else '➡️ Token atual mantido'}\n"
                f"{pool_info}"
                f"💬 {result.get('refresh_message', '')}\n\n"
                f"Teste enviando uma mensagem!",
                parse_mode='Markdown'
            )
        else:
            await msg.edit_text(f"❌ Erro: {result.get('error', result.get('message', 'Desconhecido'))}")
    except Exception as e:
        await msg.edit_text(f"❌ Erro de conexão: {e}")


async def cmd_tokencolar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Permite colar o __Secure-next-auth.session-token diretamente"""
    user_id = update.effective_user.id
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado.")
        return
    
    # Se veio com argumento, processa direto
    if context.args:
        token = ' '.join(context.args).strip()
        msg = await update.message.reply_text("🔄 *Processando token...*", parse_mode='Markdown')
        try:
            await update.message.delete()
        except: pass
        
        await _process_token(msg, token)
        return
    
    # Sem argumento: entra em modo de espera
    context.user_data['waiting_for_token'] = True
    await update.message.reply_text(
        "🔑 *Colar Token*\n\n"
        "Cole o valor do `__Secure-next-auth.session-token` aqui.\n\n"
        "📋 *Como obter:*\n"
        "1. Abra `perplexity.ai` no navegador\n"
        "2. F12 → Application → Cookies\n"
        "3. Copie o valor de `__Secure-next-auth.session-token`\n"
        "4. Cole aqui\n\n"
        "⚠️ _A mensagem será deletada por segurança._\n"
        "_Digite /cancelar para abortar._",
        parse_mode='Markdown'
    )


async def cmd_colarcookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Permite colar a string de cookies do DevTools (Network → Cookie header)"""
    user_id = update.effective_user.id
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado.")
        return
    
    # Se veio com argumento, processa direto
    if context.args:
        cookie_string = ' '.join(context.args).strip()
        msg = await update.message.reply_text("🔄 *Processando cookies...*", parse_mode='Markdown')
        try:
            await update.message.delete()
        except: pass
        
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=_mcp_headers()) as client:
                resp = await client.post(
                    f"{MCP_API}/tokens/upload_cookies",
                    json={"cookie_string": cookie_string}
                )
                result = resp.json()
            
            status = result.get('status', 'error')
            if status == 'success':
                await msg.edit_text(
                    f"✅ *Cookies Processados!*\n\n"
                    f"🍪 {result.get('cookies_count', 0)} cookies salvos\n"
                    f"🔑 Session token: {'✅ Encontrado e ativado!' if result.get('has_session_token') else '❌ Não encontrado'}\n"
                    f"🔄 Refresh: {result.get('refresh_result', 'N/A')}\n\n"
                    f"Teste enviando uma mensagem!",
                    parse_mode='Markdown'
                )
            elif status == 'warning':
                await msg.edit_text(
                    f"⚠️ *Cookies Salvos (sem session token)*\n\n"
                    f"🍪 {result.get('cookies_count', 0)} cookies salvos\n"
                    f"❌ `__Secure-next-auth.session-token` não encontrado!\n\n"
                    f"📋 Nomes encontrados:\n"
                    + '\n'.join(f"  • `{n}`" for n in result.get('cookie_names', [])[:8]) +
                    f"\n\n_Use /tokencolar para colar o session token diretamente._",
                    parse_mode='Markdown'
                )
            else:
                await msg.edit_text(f"❌ Erro: {result.get('error', result.get('message', 'Desconhecido'))}")
        except Exception as e:
            await msg.edit_text(f"❌ Erro de conexão: {e}")
        return
    
    # Sem argumento: entra em modo de espera
    context.user_data['waiting_for_cookies'] = True
    await update.message.reply_text(
        "🍪 *Colar Cookies*\n\n"
        "Cole a string de cookies do DevTools aqui.\n\n"
        "📋 *Como obter:*\n"
        "1. Abra `perplexity.ai` no navegador\n"
        "2. F12 → Network → clique em qualquer request\n"
        "3. Headers → copie o valor de `Cookie:`\n"
        "4. Cole aqui\n\n"
        "⚠️ _A mensagem será deletada por segurança._\n"
        "_Digite /cancelar para abortar._",
        parse_mode='Markdown'
    )


async def cmd_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instrui o usuário a enviar arquivo JSON de cookies"""
    user_id = update.effective_user.id
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado.")
        return
    
    context.user_data['waiting_for_cookies_file'] = True
    await update.message.reply_text(
        "📁 *Upload de Cookies*\n\n"
        "Envie o arquivo `.json` com os cookies exportados.\n\n"
        "📋 *Como exportar:*\n"
        "1. Instale a extensão *Cookie-Editor* ou *EditThisCookie*\n"
        "2. Abra `perplexity.ai`\n"
        "3. Exporte todos os cookies como JSON\n"
        "4. Envie o arquivo aqui\n\n"
        "⚠️ *Importante:* O arquivo PRECISA conter o cookie\n"
        "`__Secure-next-auth.session-token` (httpOnly).\n"
        "Extensões normais podem não exportá-lo.\n\n"
        "_Alternativa: use /tokencolar ou /colarcookies._\n"
        "_Digite /cancelar para abortar._",
        parse_mode='Markdown'
    )


# ============= COMANDO /cancelar =============

async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancela operações em andamento"""
    waiting_keys = [
        'waiting_for_email', 'waiting_for_otp', 'waiting_for_token',
        'waiting_for_cookies', 'waiting_for_cookies_file'
    ]
    was_waiting = any(context.user_data.get(k) for k in waiting_keys)
    
    if was_waiting:
        for k in waiting_keys:
            context.user_data[k] = False
        context.user_data['refresh_email'] = None
        await update.message.reply_text("🚫 Operação cancelada. Estado limpo.")
    else:
        await update.message.reply_text("Nada para cancelar.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processa mensagens de texto (e arquivos pendentes se houver)"""
    user_id = update.effective_user.id
    
    # Controle de acesso
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado. Contate o administrador.")
        return
    
    user_query = update.message.text
    text = user_query
    config = get_user_config(user_id)
    
    # ---------------------------------------------------------
    # FLUXO DE REFRESH TOKEN (EMAIL/OTP)
    # ---------------------------------------------------------
    
    # Nota: Comandos como /cancelar são tratados pelos seus próprios handlers
    # porque este handler usa filtro ~filters.COMMAND no main().

    # Verifica explicitamente se é True (não None ou False)
    if context.user_data.get('waiting_for_email') is True:
        email = text.strip()
        # Validação simples de email
        if '@' not in email or '.' not in email:
            await update.message.reply_text("❌ Email inválido. Tente novamente ou use /cancelar.")
            return

        msg = await update.message.reply_text("🔄 Enviando código de verificação... (isso pode levar alguns segundos)")
        
        try:
            # Chama script de refresh (send-only)
            import subprocess
            # Usa sys.executable para garantir que usa o mesmo python
            cmd = [sys.executable, "scripts/refresh_token.py", "--email", email, "--send-only"]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                await msg.edit_text(
                    f"✅ Código enviado para `{email}`!\n\n"
                    "📬 Verifique seu email e digite o código OTP (6 dígitos) ou cole o Magic Link aqui:",
                    parse_mode='Markdown'
                )
                context.user_data['waiting_for_email'] = False
                context.user_data['waiting_for_otp'] = True
                context.user_data['refresh_email'] = email
            else:
                err_msg = stderr.decode()
                logger.error(f"Erro refresh send: {err_msg}")
                await msg.edit_text(f"❌ Erro ao enviar código. Verifique se o email está correto.\n\n_Erro: {err_msg.splitlines()[-1] if err_msg else 'Desconhecido'}_", parse_mode='Markdown')
        except Exception as e:
            await msg.edit_text(f"❌ Erro interno: {e}")
        return

    if context.user_data.get('waiting_for_otp') is True:
        otp = text.strip()
        email = context.user_data.get('refresh_email')
        
        msg = await update.message.reply_text("🔐 Validando código e gerando token...")
        
        try:
            # Chama script para validar e salvar
            import subprocess
            cmd = [sys.executable, "scripts/refresh_token.py", "--email", email, "--otp", otp]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                # Script salvou token no cookies.json, browser_cookies.json e pool.
                # Agora ativa no sistema em execução via endpoint /tokens/set
                stdout_text = stdout.decode()
                
                # Extrai preview do stdout (procura "Preview: ...XXXXXXXX")
                preview = "****"
                for line in stdout_text.splitlines():
                    if "Preview:" in line:
                        preview = line.split("Preview:")[-1].strip()
                        break
                
                # Recarrega tokens no sistema via API
                try:
                    async with httpx.AsyncClient(timeout=10.0, headers=_mcp_headers()) as client:
                        # Força reload do token via endpoint de reload
                        resp = await client.post(f"{MCP_API}/tokens/reload")
                        pool_resp = await client.get(f"{MCP_API}/tokens/pool/status")
                        pool = pool_resp.json() if pool_resp.status_code == 200 else {}
                    
                    pool_info = f"📊 Pool: {pool.get('valid', '?')}/{pool.get('total', '?')} válidos\n" if pool.get('total') else ""
                except Exception as e:
                    logger.error(f"Erro reload após OTP: {e}")
                    pool_info = ""
                
                await msg.edit_text(
                    f"✅ *Token Gerado com Sucesso via OTP!*\n\n"
                    f"🔑 Preview: `{preview}`\n"
                    f"📧 Email: `{email}`\n"
                    f"{pool_info}\n"
                    f"O novo token foi salvo e adicionado ao pool.\n"
                    f"Use `/token` para verificar o status.",
                    parse_mode='Markdown'
                )
            else:
                err_msg = stderr.decode()
                logger.error(f"Erro refresh otp: {err_msg}")
                await msg.edit_text(f"❌ Código inválido ou erro na validação.\n\n_Erro: {err_msg.splitlines()[-1] if err_msg else 'Desconhecido'}_", parse_mode='Markdown')
        except Exception as e:
            await msg.edit_text(f"❌ Erro interno: {e}")
        
        context.user_data['waiting_for_otp'] = False
        return

    # ---------------------------------------------------------
    # FLUXO DE TOKEN COLADO (/tokencolar)
    # ---------------------------------------------------------
    if context.user_data.get('waiting_for_token') is True:
        token = text.strip()
        context.user_data['waiting_for_token'] = False
        
        try:
            await update.message.delete()
        except: pass
        
        msg = await update.effective_chat.send_message("🔄 *Processando token...*", parse_mode='Markdown')
        await _process_token(msg, token)
        return

    # ---------------------------------------------------------
    # FLUXO DE COOKIES COLADOS (/colarcookies)
    # ---------------------------------------------------------
    if context.user_data.get('waiting_for_cookies') is True:
        cookie_string = text.strip()
        context.user_data['waiting_for_cookies'] = False
        
        # Deleta a mensagem (segurança)
        try:
            await update.message.delete()
        except: pass
        
        msg = await update.effective_chat.send_message("🔄 *Processando cookies...*", parse_mode='Markdown')
        
        try:
            async with httpx.AsyncClient(timeout=20.0, headers=_mcp_headers()) as client:
                resp = await client.post(
                    f"{MCP_API}/tokens/upload_cookies",
                    json={"cookie_string": cookie_string}
                )
                result = resp.json()
            
            status = result.get('status', 'error')
            if status == 'success':
                await msg.edit_text(
                    f"✅ *Cookies Processados!*\n\n"
                    f"🍪 {result.get('cookies_count', 0)} cookies salvos\n"
                    f"🔑 Session token: {'✅ Ativado!' if result.get('has_session_token') else '❌ Não encontrado'}\n"
                    f"🔄 Refresh: {result.get('refresh_result', 'N/A')}\n\n"
                    f"Teste enviando uma mensagem!",
                    parse_mode='Markdown'
                )
            elif status == 'warning':
                names = result.get('cookie_names', [])
                names_text = '\n'.join(f"  • `{n}`" for n in names[:8]) if names else '  Nenhum'
                await msg.edit_text(
                    f"⚠️ *Cookies Salvos (sem session token)*\n\n"
                    f"🍪 {result.get('cookies_count', 0)} cookies\n"
                    f"❌ `__Secure-next-auth.session-token` não encontrado\n\n"
                    f"📋 Nomes:\n{names_text}\n\n"
                    f"_Use /tokencolar para colar o token diretamente._",
                    parse_mode='Markdown'
                )
            else:
                await msg.edit_text(f"❌ {result.get('error', 'Erro desconhecido')}")
        except Exception as e:
            await msg.edit_text(f"❌ Erro: {e}")
        return
    # ---------------------------------------------------------

    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # Verifica se há arquivos pendentes para processar em batch
    if user_id in pending_files and len(pending_files[user_id]) > 0:
        # Processa em batch
        files_to_process = pending_files.pop(user_id)  # Remove do buffer
        await process_files_batch(update, context, user_query, files_to_process, config)
        return
    
    # Fluxo normal (sem arquivos pendentes)
    payload = {
        "query": user_query,
        "user_id": str(user_id),
        "model": _resolve_model_id(config['model'], config['reasoning']),
        "focus": config['focus'],
        "time_range": config.get('time_range', 'all'),
        "enable_reasoning": config['reasoning'],
        "citation_mode": "markdown"
    }
    
    # Adiciona coordenadas se existirem na config
    if 'lat' in config and 'lon' in config:
        payload['lat'] = config['lat']
        payload['lon'] = config['lon']
    
    # Usa a nova função de streaming
    await stream_search_and_reply(update, context, payload)


async def process_files_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                               query: str, files: list, config: dict) -> None:
    """Processa múltiplos arquivos em uma única request"""
    user_id = update.effective_user.id
    
    msg = await update.message.reply_text(
        f"🔄 *Processando {len(files)} arquivo(s)...*",
        parse_mode='Markdown'
    )
    
    try:
        async with httpx.AsyncClient(timeout=300.0, headers=_mcp_headers()) as client:
            # Metadados
            data_payload = {
                "query": query,
                "user_id": str(user_id),
                "model": _resolve_model_id(config['model'], config.get('reasoning', False)),
                "focus": "web",
                "return_citations": "false"
            }
            
            # Monta lista de arquivos para multipart
            # Formato: [('file', (nome, bytes, mime)), ('file', ...), ...]
            files_payload = [
                ('file', (f['name'], f['bytes'], f['mime'])) 
                for f in files
            ]
            
            response = await client.post(
                f"{MCP_API}/search",
                data=data_payload,
                files=files_payload
            )
            
            if response.status_code != 200:
                logger.error(f"Erro MCP batch: {response.text}")
                await msg.edit_text(f"❌ Erro ao processar arquivos: {response.status_code}")
                return
            
            data = response.json()
        
        answer = data.get('answer', 'Sem resposta')
        clean_answer = await extract_and_send_files(update, answer)
        
        await msg.delete()
        await reply_chunked(update, clean_answer)
        
    except Exception as e:
        logger.error(f"Erro batch upload: {e}")
        await msg.edit_text(f"❌ Erro: {e}")



# ============= HANDLER DE IMAGENS =============

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processa imagens enviadas pelo usuário"""
    user_id = update.effective_user.id
    
    # Controle de acesso
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado.")
        return
    
    config = get_user_config(user_id)
    
    caption = update.message.caption or "O que você vê nesta imagem?"
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        # Download da imagem
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Converte para base64
        photo_b64 = base64.b64encode(photo_bytes).decode()
        
        # Chama MCP API com imagem
        async with httpx.AsyncClient(timeout=180.0, headers=_mcp_headers()) as client:
            payload = {
                "query": caption,
                "model": _resolve_model_id(config['model'], config.get('reasoning', False)),
                "image_base64": photo_b64,
                "focus": "web"
            }
            
            response = await client.post(f"{MCP_API}/vision", json=payload)
            response.raise_for_status()
            data = response.json()
        
        answer = data.get('answer', 'Sem resposta')
        await update.message.reply_text(answer, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Erro ao processar imagem: {e}")
        await update.message.reply_text(
            "❌ Erro ao analisar imagem. Tente novamente ou use outro modelo.",
            parse_mode='Markdown'
        )


# ============= HANDLER DE DOCUMENTOS =============

MAX_PENDING_FILES = 9
FILE_TIMEOUT_SECONDS = 120  # 2 minutos

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acumula arquivos no buffer. Processa quando usuário enviar texto."""
    user_id = update.effective_user.id
    
    # Controle de acesso
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado.")
        return
    
    document = update.message.document
    file_name = document.file_name or f"arquivo_{document.file_id}"
    mime_type = document.mime_type or "application/octet-stream"
    
    # ---- Intercepta upload de cookies JSON (/cookies) ----
    if context.user_data.get('waiting_for_cookies_file') and file_name.lower().endswith('.json'):
        context.user_data['waiting_for_cookies_file'] = False
        msg = await update.message.reply_text("🔄 *Processando arquivo de cookies...*", parse_mode='Markdown')
        
        try:
            telegram_file = await document.get_file()
            file_bytes = await telegram_file.download_as_bytearray()
            cookies_data = json.loads(file_bytes.decode('utf-8'))
            
            if not isinstance(cookies_data, list):
                await msg.edit_text("❌ Formato inválido. O arquivo deve conter um array JSON de cookies.")
                return
            
            async with httpx.AsyncClient(timeout=20.0, headers=_mcp_headers()) as client:
                resp = await client.post(
                    f"{MCP_API}/tokens/upload_cookies",
                    json={"cookies": cookies_data}
                )
                result = resp.json()
            
            status = result.get('status', 'error')
            if status == 'success':
                await msg.edit_text(
                    f"✅ *Cookies do Arquivo Processados!*\n\n"
                    f"🍪 {result.get('cookies_count', 0)} cookies salvos\n"
                    f"🔑 Session token: {'✅ Ativado!' if result.get('has_session_token') else '❌ Não encontrado'}\n"
                    f"🔄 Refresh: {result.get('refresh_result', 'N/A')}\n\n"
                    f"Teste enviando uma mensagem!",
                    parse_mode='Markdown'
                )
            elif status == 'warning':
                names = result.get('cookie_names', [])
                names_text = '\n'.join(f"  • `{n}`" for n in names[:8]) if names else '  Nenhum'
                await msg.edit_text(
                    f"⚠️ *Cookies Salvos (sem session token)*\n\n"
                    f"🍪 {result.get('cookies_count', 0)} cookies do arquivo\n"
                    f"❌ `__Secure-next-auth.session-token` não encontrado\n\n"
                    f"📋 Nomes:\n{names_text}\n\n"
                    f"_Use /tokencolar para colar o token diretamente._",
                    parse_mode='Markdown'
                )
            else:
                await msg.edit_text(f"❌ {result.get('error', 'Erro desconhecido')}")
        except json.JSONDecodeError:
            await msg.edit_text("❌ Arquivo JSON inválido. Verifique o formato.")
        except Exception as e:
            await msg.edit_text(f"❌ Erro: {e}")
        return
    
    # Lista negra de extensões perigosas
    BLOCKED_EXTENSIONS = ('.exe', '.bat', '.cmd', '.sh', '.bin')
    if file_name.lower().endswith(BLOCKED_EXTENSIONS):
        await update.message.reply_text("⚠️ Tipo de arquivo não permitido por segurança.")
        return
    
    try:
        # Download do arquivo
        telegram_file = await document.get_file()
        file_bytes = await telegram_file.download_as_bytearray()
        
        # Inicializa buffer se não existe
        if user_id not in pending_files:
            pending_files[user_id] = []
        
        # Limpa arquivos antigos (timeout)
        current_time = time.time()
        pending_files[user_id] = [
            f for f in pending_files[user_id] 
            if current_time - f['timestamp'] < FILE_TIMEOUT_SECONDS
        ]
        
        # Verifica limite
        if len(pending_files[user_id]) >= MAX_PENDING_FILES:
            await update.message.reply_text(
                f"⚠️ Limite de {MAX_PENDING_FILES} arquivos atingido.\n"
                "Envie sua pergunta para processar ou use /limpar para recomeçar."
            )
            return
        
        # Adiciona ao buffer
        pending_files[user_id].append({
            'name': file_name,
            'bytes': bytes(file_bytes),
            'mime': mime_type,
            'timestamp': current_time
        })
        
        count = len(pending_files[user_id])
        file_list = "\n".join([f"  • {f['name'].replace('_', ' ')}" for f in pending_files[user_id]])
        
        await update.message.reply_text(
            f"📎 {count} arquivo(s) recebido(s):\n{file_list}\n\n"
            f"Envie mais arquivos (até {MAX_PENDING_FILES}) ou digite sua pergunta para processar."
        )
        
    except Exception as e:
        logger.error(f"Erro ao receber documento: {e}")
        await update.message.reply_text(f"❌ Erro ao receber arquivo: {e}")


# ============= HANDLER DE ÁUDIO (Voice Notes) =============

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Áudio vai direto ao MCP via multipart upload"""
    user_id = update.effective_user.id
    
    # Controle de acesso
    if not _check_access(user_id):
        await update.message.reply_text("🔒 Acesso não autorizado.")
        return
    
    config = get_user_config(user_id)
    
    # Pega o áudio
    if update.message.voice:
        audio = update.message.voice
        file_name = "audio.ogg"
        mime_type = "audio/ogg"
    elif update.message.audio:
        audio = update.message.audio
        ext = (audio.file_name or "audio.mp3").split('.')[-1]
        file_name = f"audio.{ext}"
        mime_type = audio.mime_type or "audio/mpeg"
    else:
        return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        # Download
        telegram_file = await audio.get_file()
        audio_bytes = await telegram_file.download_as_bytearray()
        
        # Envia via multipart/form-data (como MCP espera)
        async with httpx.AsyncClient(timeout=180.0, headers=_mcp_headers()) as client:
            files = {"file": (file_name, bytes(audio_bytes), mime_type)}
            data = {
                "query": ".",
                "user_id": str(user_id),
                "model": _resolve_model_id(config['model'], config.get('reasoning', False)),
                "focus": config['focus']
            }
            
            response = await client.post(f"{MCP_API}/search", data=data, files=files)
            response.raise_for_status()
            result = response.json()
        
        answer = result.get('answer', '')
        if answer:
            await update.message.reply_text(answer)
        
    except Exception as e:
        logger.error(f"Erro áudio: {e}")
        await update.message.reply_text(f"❌ Erro: {e}")


# ============= POST INIT =============

async def post_init(application: Application) -> None:
    """Executado após a inicialização da aplicação, dentro do loop de eventos"""
    logger.info("🚀 Executando post_init...")
    
    # Inicia Scheduler (agora que temos loop)
    tm = get_task_manager()
    if tm and tm.scheduler:
        try:
            tm.scheduler.start()
            logger.info("📅 APScheduler iniciado com sucesso!")
        except Exception as e:
            logger.warning(f"⚠️ Scheduler já rodando ou erro: {e}")

    # Define comandos
    await application.bot.set_my_commands([
        BotCommand("start", "Menu Principal"),
        BotCommand("busca", "Nova Busca"),
        BotCommand("new", "Nova Conversa"),
        BotCommand("vpn", "Controle VPN"),
        BotCommand("tarefas", "Gerenciar Tarefas"),
        BotCommand("credencial", "Tokens e Cookies"),
        BotCommand("modelos", "Trocar Modelo"),
        BotCommand("config", "Configurações"),
        BotCommand("voz", "Escolher Voz TTS"),
        BotCommand("ajuda", "Ajuda")
    ])
    logger.info("✅ Comandos registrados no Telegram")


# ============= MAIN =============

def main() -> None:
    """Inicia o bot"""
    logger.info("🚀 Iniciando Perplexo Bot...")

    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "seu_token_aqui":
        logger.error("TELEGRAM_TOKEN não configurado. Configure no .env para iniciar o bot.")
        return
    
    # Inicializa APScheduler se disponível
    scheduler = None
    if SCHEDULER_AVAILABLE:
        scheduler = AsyncIOScheduler()
        # scheduler.start()  <-- Removido: Seráiciado no post_init
        logger.info("📅 APScheduler inicializado (aguardando start)")
    
    # Callback para executar tarefas agendadas
    async def execute_scheduled_task(user_id: int, task: Task):
        """Callback executado pelo scheduler quando uma tarefa dispara"""
        logger.info(f"⏰ Executando tarefa agendada: {task.name} para user {user_id}")
        try:
            async with httpx.AsyncClient(timeout=60.0, headers=_mcp_headers()) as client:
                response = await client.post(
                    f"{MCP_API}/search_stream",
                    json={
                        "query": task.prompt,
                        "user_id": str(user_id),
                        "model": task.model,
                        "focus": "web"
                    }
                )
                
                if response.status_code == 200:
                    # Processa resposta do stream
                    full_answer = ""
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            try:
                                data = json.loads(line[6:])
                                if "chunk" in data:
                                    full_answer += data['chunk']
                                if "answer" in data:
                                    full_answer = data['answer']
                            except:
                                pass
                    
                    # Envia notificação via Telegram
                    from telegram import Bot
                    bot = Bot(token=TELEGRAM_TOKEN)
                    msg_text = f"📋 *Tarefa: {task.name}*\n\n{full_answer[:3900]}"
                    await bot.send_message(
                        chat_id=user_id,
                        text=msg_text,
                        parse_mode='Markdown'
                    )
                    logger.info(f"✅ Notificação enviada para {user_id}")
        except Exception as e:
            logger.error(f"Erro ao executar tarefa {task.task_id}: {e}")
    
    # Inicializa TaskManager com scheduler
    init_task_manager(scheduler=scheduler, execute_callback=execute_scheduled_task)
    logger.info("📋 TaskManager inicializado")
    
    # Cria aplicação
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("modelos", cmd_modelos))
    app.add_handler(CommandHandler("busca", cmd_busca))
    app.add_handler(CommandHandler("denovo", cmd_denovo))
    app.add_handler(CommandHandler("tarefas", cmd_tarefas))
    app.add_handler(CommandHandler("local", cmd_local))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("library", cmd_library))
    app.add_handler(CommandHandler("vpn", cmd_vpn))
    app.add_handler(CommandHandler("token", cmd_token))
    app.add_handler(CommandHandler("credencial", cmd_token))
    app.add_handler(CommandHandler("credenciais", cmd_token))
    app.add_handler(CommandHandler("credentials", cmd_token))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))

    app.add_handler(CommandHandler("historico", cmd_historico))
    app.add_handler(CommandHandler("teste", cmd_teste))
    app.add_handler(CommandHandler("importar", cmd_importar))
    app.add_handler(CommandHandler("normal", cmd_normal))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("limpar", cmd_limpar))
    app.add_handler(CommandHandler("tempo", cmd_tempo))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("voz", cmd_voz))
    app.add_handler(CommandHandler("tokencolar", cmd_tokencolar))
    app.add_handler(CommandHandler("colarcookies", cmd_colarcookies))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("apagartokens", cmd_apagartokens))
    
    # Callbacks (botões inline)
    app.add_handler(CallbackQueryHandler(token_callback_handler, pattern="^token_"))
    app.add_handler(CallbackQueryHandler(token_callback_handler, pattern="^pool_"))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Mensagens
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    
    # Webhook ou Polling
    if WEBHOOK_URL:
        logger.info(f"📡 Modo Webhook: {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=TELEGRAM_PORT,
            webhook_url=WEBHOOK_URL,
            url_path="/telegram"
        )
    else:
        logger.info("🔄 Modo Polling (desenvolvimento)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
