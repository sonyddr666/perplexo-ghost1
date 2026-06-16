"""
TTS Service - Inworld AI Text-to-Speech
========================================
Gera áudio a partir de texto usando a API Inworld AI TTS.
Integração com Firebase para autenticação automática.

Fluxo de autenticação:
    FIREBASE_REFRESH_TOKEN → Firebase JWT → Inworld TTS Token (~1h)

Uso:
    from tts_service import tts_generate_audio, tts_extract_and_generate

    # Gerar áudio direto
    audio_bytes = await tts_generate_audio("Olá mundo")

    # Extrair RESPOSTASIMPLES:(((texto))) e gerar áudio
    clean_text, audio_bytes = await tts_extract_and_generate(resposta_completa)
"""

import os
import re
import json
import time
import base64
import random
import logging
import asyncio
from typing import Optional, Tuple, List, Dict
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ============= CONFIGURAÇÕES TTS =============

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "AIzaSyAPVBLVid0xPwjuU4Gmn_6_GyqxBq-SwQs")
FIREBASE_REFRESH_TOKEN = os.getenv("FIREBASE_REFRESH_TOKEN", "")
WORKSPACE_ID = os.getenv("TTS_WORKSPACE_ID", "default--pb4bm1oowkem_r9ri2wiw")
DEFAULT_VOICE = os.getenv("TTS_VOICE_ID", "default--pb4bm1oowkem_r9ri2wiw__sony")
DEFAULT_MODEL = os.getenv("TTS_MODEL", "inworld-tts-1.5-max")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
TTS_PITCH = float(os.getenv("TTS_PITCH", "0.0"))
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower() in ("true", "1", "yes")

BASE_URL = "https://api.inworld.ai"
MAX_CHARS = 2000  # Limite da API Inworld

# Regex para capturar RESPOSTA_SIMPLES:(((texto))) ou RESPOSTASIMPLES:(((texto)))
TTS_PATTERN = re.compile(r'RESPOSTA_?SIMPLES:\s*\(\(\((.*?)\)\)\)', re.DOTALL)

# User-Agents para rotação (anti-detecção)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/144.0.0.0 Safari/537.36",
]

# ============= ESTADO GLOBAL DO TOKEN =============

_current_token: Optional[str] = os.getenv("INWORLD_TOKEN", "")
_token_expiry: float = 0  # timestamp de expiração


def _is_token_valid() -> bool:
    """Verifica se o token atual ainda é válido (com margem de 5 min)"""
    if not _current_token:
        return False
    if _token_expiry > 0 and time.time() > (_token_expiry - 300):
        return False
    # Se não sabemos a expiração, tenta usar
    return True


def _refresh_firebase_token() -> Optional[str]:
    """Renova o accessToken usando o refreshToken do Firebase"""
    if not FIREBASE_REFRESH_TOKEN:
        logger.error("❌ TTS: FIREBASE_REFRESH_TOKEN não configurado")
        return None
    try:
        url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
        payload = {"grant_type": "refresh_token", "refresh_token": FIREBASE_REFRESH_TOKEN}
        response = requests.post(url, data=payload, timeout=30)
        if response.status_code == 200:
            token = response.json().get("id_token")
            logger.info("✅ TTS: Firebase token renovado")
            return token
        else:
            logger.error(f"❌ TTS: Firebase refresh falhou: {response.status_code}")
    except Exception as e:
        logger.error(f"❌ TTS: Erro ao renovar Firebase token: {e}")
    return None


def _refresh_inworld_token() -> Optional[str]:
    """Gera novo token Inworld TTS usando Firebase JWT"""
    global _current_token, _token_expiry

    firebase_token = _refresh_firebase_token()
    if not firebase_token:
        return None

    try:
        url = f"https://platform.inworld.ai/ai/inworld/portal/v1alpha/workspaces/{WORKSPACE_ID}/token:generate"
        headers = {
            "authorization": f"Bearer {firebase_token}",
            "content-type": "text/plain;charset=UTF-8",
            "grpc-metadata-x-authorization-bearer-type": "firebase",
            "origin": "https://platform.inworld.ai",
            "referer": f"https://platform.inworld.ai/v2/workspaces/{WORKSPACE_ID}/tts-playground",
        }
        payload = json.dumps({})
        response = requests.post(url, headers=headers, data=payload, timeout=30)

        if response.status_code == 200:
            data = response.json()
            new_token = data.get("token")
            expiration = data.get("expirationTime", "")
            if new_token:
                _current_token = new_token
                # Parse expiração ISO → timestamp
                try:
                    from datetime import datetime
                    exp_dt = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
                    _token_expiry = exp_dt.timestamp()
                except Exception:
                    _token_expiry = time.time() + 3500  # ~58 min fallback
                logger.info(f"✅ TTS: Token Inworld renovado (expira: {expiration})")
                return new_token
        else:
            logger.error(f"❌ TTS: Erro ao gerar token Inworld: {response.status_code} - {response.text[:200]}")
    except Exception as e:
        logger.error(f"❌ TTS: Erro ao gerar token Inworld: {e}")
    return None


def _ensure_token() -> Optional[str]:
    """Garante que temos um token válido, renovando se necessário"""
    global _current_token
    if _is_token_valid():
        return _current_token
    logger.info("🔄 TTS: Token expirado ou ausente, renovando...")
    return _refresh_inworld_token()


def _get_headers() -> dict:
    """Headers para a API Inworld TTS"""
    return {
        "Authorization": f"Bearer {_current_token}",
        "Content-Type": "application/json",
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://platform.inworld.ai",
        "Referer": "https://platform.inworld.ai/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }


def _generate_audio_sync(
    text: str,
    voice_id: str = None,
    model_id: str = None,
    speed: float = None,
    pitch: float = None,
) -> Optional[bytes]:
    """
    Gera áudio MP3 via API TTS (síncrono).
    Retorna bytes do MP3 ou None em caso de erro.
    """
    voice_id = voice_id or DEFAULT_VOICE
    model_id = model_id or DEFAULT_MODEL
    speed = speed if speed is not None else TTS_SPEED
    pitch = pitch if pitch is not None else TTS_PITCH

    # Garante token válido
    token = _ensure_token()
    if not token:
        logger.error("❌ TTS: Sem token válido para gerar áudio")
        return None

    # Trunca se necessário
    if len(text) > MAX_CHARS:
        logger.warning(f"⚠️ TTS: Texto truncado de {len(text)} para {MAX_CHARS} chars")
        text = text[:MAX_CHARS]

    url = f"{BASE_URL}/tts/v1/voice"
    payload = {
        "text": text,
        "voice_id": voice_id,
        "model_id": model_id,
        "audio_config": {
            "audio_encoding": "MP3",
            "speaking_rate": speed,
            "pitch": pitch,
            "sample_rate_hertz": 48000,
        },
        "temperature": 1.0,
    }

    logger.info(f"🎙️ TTS: Gerando áudio ({model_id}): '{text[:40]}...'")

    # Delay anti-detecção
    time.sleep(random.uniform(0.2, 0.5))

    try:
        response = requests.post(url, headers=_get_headers(), json=payload, timeout=60)

        # Se 401/403, tenta renovar token e retry
        if response.status_code in (401, 403):
            logger.warning("🔄 TTS: Token expirado, renovando e tentando novamente...")
            new_token = _refresh_inworld_token()
            if not new_token:
                return None
            response = requests.post(url, headers=_get_headers(), json=payload, timeout=60)

        if response.status_code != 200:
            logger.error(f"❌ TTS: API erro {response.status_code}: {response.text[:300]}")
            return None

        data = response.json()
        if "audioContent" not in data:
            logger.error(f"❌ TTS: Resposta sem audioContent: {list(data.keys())}")
            return None

        audio_bytes = base64.b64decode(data["audioContent"])
        if len(audio_bytes) < 100:
            logger.error("❌ TTS: Áudio retornado está vazio/corrompido")
            return None

        logger.info(f"✅ TTS: Áudio gerado ({len(audio_bytes) / 1024:.1f} KB)")
        return audio_bytes

    except requests.Timeout:
        logger.error("❌ TTS: Timeout na API")
    except Exception as e:
        logger.error(f"❌ TTS: Erro na geração: {e}")
    return None


# ============= FUNÇÕES ASYNC (para usar no Telegram bot) =============

async def tts_generate_audio(
    text: str,
    voice_id: str = None,
    model_id: str = None,
    speed: float = None,
    pitch: float = None,
) -> Optional[bytes]:
    """
    Gera áudio MP3 a partir do texto (async wrapper).
    Retorna bytes do MP3 ou None.
    """
    if not TTS_ENABLED:
        logger.debug("TTS desabilitado (TTS_ENABLED=false)")
        return None

    if not text or not text.strip():
        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _generate_audio_sync, text, voice_id, model_id, speed, pitch
    )


def tts_extract_simple_response(text: str) -> Tuple[str, Optional[str]]:
    """
    Extrai o texto de RESPOSTA_SIMPLES:(((texto))) ou RESPOSTASIMPLES:(((texto))) da resposta.

    Retorna:
        (texto_limpo_sem_o_marcador, texto_para_tts_ou_None)
    """
    match = TTS_PATTERN.search(text)
    if not match:
        return text, None

    tts_text = match.group(1).strip()
    # Remove o marcador completo do texto de exibição
    clean_text = text[:match.start()] + tts_text + text[match.end():]
    clean_text = clean_text.strip()

    logger.info(f"🎙️ TTS: Padrão RESPOSTA_SIMPLES detectado, texto extraído: '{tts_text[:80]}...'")
    return clean_text, tts_text


async def tts_extract_and_generate(text: str, voice_id: str = None) -> Tuple[str, Optional[bytes]]:
    """
    1. Procura RESPOSTASIMPLES:(((texto))) na resposta
    2. Extrai o texto
    3. Gera áudio apenas dessa parte
    4. Retorna (texto_limpo, audio_bytes_ou_None)
    """
    clean_text, tts_text = tts_extract_simple_response(text)

    if not tts_text:
        return text, None

    logger.info(f"🎙️ TTS: Encontrado RESPOSTASIMPLES, gerando áudio para: '{tts_text[:50]}...'")
    audio = await tts_generate_audio(tts_text, voice_id=voice_id)
    return clean_text, audio


# ============= ENDPOINT PARA MCP SERVER =============

def tts_generate_sync(text: str) -> Optional[bytes]:
    """Versão síncrona para uso no Flask MCP server"""
    if not TTS_ENABLED:
        return None
    if not text or not text.strip():
        return None
    return _generate_audio_sync(text)


# ============= STATUS =============

# Mapa de idiomas para filtro
TTS_IDIOMAS = {
    'pt': '🇧🇷 Português',
    'en': '🇺🇸 English',
    'es': '🇪🇸 Español',
    'fr': '🇫🇷 Français',
    'de': '🇩🇪 Deutsch',
    'it': '🇮🇹 Italiano',
    'nl': '🇳🇱 Nederlands',
    'pl': '🇵🇱 Polski',
    'ru': '🇷🇺 Русский',
    'zh': '🇨🇳 中文',
    'ja': '🇯🇵 日本語',
    'ko': '🇰🇷 한국어',
    'hi': '🇮🇳 हिन्दी',
}

# Cache de vozes
_voices_cache: List[Dict] = []
_voices_cache_time: float = 0
_VOICES_CACHE_TTL = 300  # 5 minutos


def tts_fetch_voices(filtro_idioma: str = None) -> List[Dict]:
    """
    Lista vozes disponíveis na API Inworld.
    Retorna lista de dicts com: voiceId, displayName, languageCode, etc.
    Cache de 5 minutos.
    """
    global _voices_cache, _voices_cache_time

    token = _ensure_token()
    if not token:
        logger.error("❌ TTS: Sem token para listar vozes")
        return []

    # Usa cache se válido
    if _voices_cache and (time.time() - _voices_cache_time) < _VOICES_CACHE_TTL:
        voices = _voices_cache
    else:
        url = f"{BASE_URL}/voices/v1/workspaces/{WORKSPACE_ID}/voices"
        try:
            response = requests.get(url, headers=_get_headers(), timeout=30)
            if response.status_code in (401, 403):
                _refresh_inworld_token()
                response = requests.get(url, headers=_get_headers(), timeout=30)

            response.raise_for_status()
            voices = response.json().get('voices', [])
            _voices_cache = voices
            _voices_cache_time = time.time()
            logger.info(f"📥 TTS: Carregadas {len(voices)} vozes da API")
        except Exception as e:
            logger.error(f"❌ TTS: Erro ao buscar vozes: {e}")
            return _voices_cache if _voices_cache else []

    # Filtra por idioma se especificado
    if filtro_idioma and filtro_idioma != 'all':
        filtered = []
        for v in voices:
            name = v.get('displayName', '').lower()
            lang = v.get('languageCode', '').lower()
            if filtro_idioma.lower() in name or filtro_idioma.lower() in lang:
                filtered.append(v)
        return filtered

    return voices


def tts_get_voice_name(voice_id: str) -> str:
    """Retorna nome amigável da voz a partir do ID"""
    if '__' in voice_id:
        return voice_id.split('__')[-1].title()
    return voice_id[-15:]


def tts_status() -> dict:
    """Retorna status do serviço TTS"""
    return {
        "enabled": TTS_ENABLED,
        "has_firebase_token": bool(FIREBASE_REFRESH_TOKEN),
        "has_inworld_token": bool(_current_token),
        "token_valid": _is_token_valid(),
        "voice": DEFAULT_VOICE,
        "model": DEFAULT_MODEL,
        "speed": TTS_SPEED,
        "pitch": TTS_PITCH,
        "workspace": WORKSPACE_ID,
    }
