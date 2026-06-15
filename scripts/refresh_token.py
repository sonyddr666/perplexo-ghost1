#!/usr/bin/env python3
"""
Perplexity Token Refresher
==========================
Renova automaticamente o session token do Perplexity usando autenticação por email.

Baseado no CLI do perplexity-webui-scraper (henrique-coder).

Fluxo:
1. Obtém CSRF token
2. Envia código de verificação para o email
3. Valida OTP (6 dígitos) ou magic link
4. Extrai session token dos cookies
5. Salva no TokenManager

Uso:
    # Interativo (pede email e código do email)
    python scripts/refresh_token.py
    
    # Non-interactive (para automação externa)
    python scripts/refresh_token.py --email user@example.com --wait-otp
    
    # Verifica status/validade do token atual
    python scripts/refresh_token.py --check
"""

import os
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# Adiciona src ao path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

try:
    from curl_cffi.requests import Session
except ImportError:
    print("❌ curl_cffi não instalado. Execute: pip install curl-cffi")
    sys.exit(1)

try:
    import orjson as json_lib
    loads = json_lib.loads
except ImportError:
    import json as json_lib
    loads = json_lib.loads

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# ============= CONSTANTES =============

BASE_URL = "https://www.perplexity.ai"
TOKENS_DIR = Path(os.getenv("TOKENS_DIR", "./data/tokens"))
COOKIES_FILE = "cookies.json"
SESSION_CACHE_FILE = ".refresh_session_cache.json"  # Cache temporário da sessão


# ============= FUNÇÕES DE AUTENTICAÇÃO =============

def initialize_session() -> Tuple[Session, str]:
    """Inicializa sessão e obtém CSRF token."""
    logger.info("🔄 Inicializando conexão...")
    
    session = Session(
        impersonate="chrome",
        headers={"Referer": BASE_URL, "Origin": BASE_URL}
    )
    
    # Acessa a página principal primeiro
    session.get(BASE_URL)
    
    # Obtém CSRF token
    csrf_response = session.get(f"{BASE_URL}/api/auth/csrf")
    csrf_data = loads(csrf_response.content)
    csrf = csrf_data.get("csrfToken")
    
    if not csrf:
        raise ValueError("Falha ao obter CSRF token")
    
    logger.info("✅ CSRF token obtido")
    return session, csrf


def request_verification_code(session: Session, csrf: str, email: str) -> bool:
    """Envia código de verificação para o email."""
    logger.info(f"📧 Enviando código de verificação para {email}...")
    
    response = session.post(
        f"{BASE_URL}/api/auth/signin/email?version=2.18&source=default",
        json={
            "email": email,
            "csrfToken": csrf,
            "useNumericOtp": "true",
            "json": "true",
            "callbackUrl": f"{BASE_URL}/?login-source=floatingSignup",
        },
    )
    
    if response.status_code != 200:
        logger.error(f"❌ Erro ao enviar código: {response.text}")
        return False
    
    logger.info("✅ Código enviado! Verifique seu email.")
    return True


def validate_otp(session: Session, email: str, otp_or_link: str) -> Optional[str]:
    """Valida OTP ou magic link e retorna URL de redirect."""
    logger.info("🔐 Validando código...")
    
    # Se for link, usa direto
    if otp_or_link.startswith("http"):
        return otp_or_link
    
    # Se for OTP, obtém redirect URL
    response = session.post(
        f"{BASE_URL}/api/auth/otp-redirect-link",
        json={
            "email": email,
            "otp": otp_or_link,
            "redirectUrl": f"{BASE_URL}/?login-source=floatingSignup",
            "emailLoginMethod": "web-otp",
        },
    )
    
    if response.status_code != 200:
        logger.error(f"❌ Código inválido: {response.text}")
        return None
    
    redirect_path = loads(response.content).get("redirect")
    
    if not redirect_path:
        logger.error("❌ Sem URL de redirect")
        return None
    
    return f"{BASE_URL}{redirect_path}" if redirect_path.startswith("/") else redirect_path


def extract_session_token(session: Session, redirect_url: str) -> Optional[str]:
    """Extrai session token dos cookies após autenticação."""
    logger.info("🍪 Extraindo session token...")
    
    session.get(redirect_url)
    token = session.cookies.get("__Secure-next-auth.session-token")
    
    if not token:
        logger.error("❌ Token não encontrado nos cookies")
        return None
    
    logger.info("✅ Token extraído com sucesso!")
    return token


def save_token(token: str, account_name: str = "refreshed") -> bool:
    """Salva token no cookies.json, merge no browser_cookies.json e adiciona ao pool."""
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    cookies_path = TOKENS_DIR / COOKIES_FILE
    
    # 1. Salva no cookies.json
    if cookies_path.exists():
        with open(cookies_path, 'r') as f:
            data = json.load(f)
    else:
        data = {"accounts": [], "current_index": 0}
    
    data['accounts'] = [a for a in data['accounts'] if a.get('name') != account_name]
    data['accounts'].insert(0, {
        "name": account_name,
        "session_token": token,
        "refreshed_at": datetime.now().isoformat(),
        "source": "otp_refresh"
    })
    data['current_index'] = 0
    data['updated_at'] = datetime.now().isoformat()
    
    with open(cookies_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"💾 Token salvo em: {cookies_path}")
    
    # 2. Merge no browser_cookies.json (preserva cf_clearance, _bm, etc.)
    raw_path = TOKENS_DIR / "browser_cookies.json"
    try:
        cookies = []
        if raw_path.exists():
            with open(raw_path, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            if not isinstance(cookies, list):
                cookies = []
        
        found = False
        for cookie in cookies:
            if isinstance(cookie, dict) and cookie.get('name') == '__Secure-next-auth.session-token':
                cookie['value'] = token
                found = True
                break
        if not found:
            cookies.append({
                "name": "__Secure-next-auth.session-token",
                "value": token,
                "domain": ".perplexity.ai",
                "path": "/",
                "httpOnly": True,
                "secure": True
            })
        
        with open(raw_path, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        logger.info(f"🍪 Merge no browser_cookies.json ({len(cookies)} cookies)")
    except Exception as e:
        logger.warning(f"⚠️ Não foi possível fazer merge no browser_cookies.json: {e}")
    
    # 3. Adiciona ao pool
    try:
        from token_manager import TokenManager
        tm = TokenManager(tokens_dir=TOKENS_DIR)
        pool_result = tm.add_to_pool(token, name=account_name, validate=False)
        logger.info(f"📊 Token adicionado ao pool: {pool_result.get('id', '?')}")
    except Exception as e:
        logger.warning(f"⚠️ Não foi possível adicionar ao pool: {e}")
    
    return True


def update_env_file(token: str) -> bool:
    """Atualiza .env com o novo token."""
    env_path = Path(".env")
    key = "PERPLEXITY_SESSION_TOKEN"
    line_entry = f'{key}="{token}"'
    
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        updated = False
        new_lines = []
        
        for line in lines:
            if line.strip().startswith(key):
                new_lines.append(line_entry)
                updated = True
            else:
                new_lines.append(line)
        
        if not updated:
            if new_lines and new_lines[-1] != "":
                new_lines.append("")
            new_lines.append(line_entry)
        
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        logger.info("📝 .env atualizado")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro ao atualizar .env: {e}")
        return False


# ============= FLUXO PRINCIPAL =============

def refresh_token_interactive(account_name: str = "main") -> Optional[str]:
    """Fluxo interativo completo de refresh do token."""
    print("\n" + "=" * 50)
    print("🔑 Perplexity Token Refresher")
    print("=" * 50)
    
    try:
        # Passo 1: Inicializa sessão
        session, csrf = initialize_session()
        
        # Passo 2: Pede email
        email = input("\n📧 Digite seu email do Perplexity: ").strip()
        if not email:
            logger.error("Email não pode ser vazio!")
            return None
        
        # Passo 3: Envia código
        if not request_verification_code(session, csrf, email):
            return None
        
        # Passo 4: Pede OTP
        print("\n📬 Verifique seu email para o código de 6 dígitos ou magic link")
        otp_or_link = input("Digite o código ou cole o link: ").strip()
        
        # Passo 5: Valida
        redirect_url = validate_otp(session, email, otp_or_link)
        if not redirect_url:
            return None
        
        # Passo 6: Extrai token
        token = extract_session_token(session, redirect_url)
        if not token:
            return None
        
        # Passo 7: Salva
        save_token(token, account_name)
        update_env_file(token)
        
        print("\n" + "=" * 50)
        print("✅ Token renovado com sucesso!")
        print(f"📋 Token: {token[:40]}...{token[-10:]}")
        print("=" * 50 + "\n")
        
        return token
        
    except KeyboardInterrupt:
        print("\n❌ Cancelado pelo usuário")
        return None
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None


def refresh_token_with_otp(email: str, otp: str, account_name: str = "main") -> Optional[str]:
    """
    Refresh não-interativo quando já temos o OTP.
    Útil para integração com painéis, jobs ou automações.
    """
    try:
        session, csrf = initialize_session()
        
        if not request_verification_code(session, csrf, email):
            return None
        
        redirect_url = validate_otp(session, email, otp)
        if not redirect_url:
            return None
        
        token = extract_session_token(session, redirect_url)
        if not token:
            return None
        
        save_token(token, account_name)
        update_env_file(token)
        
        return token
        
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None


def send_verification_only(email: str) -> Tuple[Optional[Session], Optional[str]]:
    """
    Apenas envia o código de verificação.
    Salva cookies da sessão em disco para reutilizar com validate_and_complete().
    """
    try:
        session, csrf = initialize_session()
        
        if request_verification_code(session, csrf, email):
            # Salva sessão em disco para reutilizar
            _save_session_cache(session, csrf, email)
            return session, csrf
        return None, None
        
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None, None


def _save_session_cache(session: Session, csrf: str, email: str):
    """Salva cookies e CSRF da sessão em disco para reutilizar."""
    cache_path = TOKENS_DIR / SESSION_CACHE_FILE
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    
    cache = {
        "csrf": csrf,
        "email": email,
        "cookies": dict(session.cookies),
        "saved_at": datetime.now().isoformat()
    }
    
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)
    logger.info(f"💾 Sessão salva em cache: {cache_path}")


def _load_session_cache() -> Optional[dict]:
    """Carrega sessão salva do disco."""
    cache_path = TOKENS_DIR / SESSION_CACHE_FILE
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        
        # Valida que não é muito antigo (max 10 min)
        saved = datetime.fromisoformat(cache.get('saved_at', ''))
        age = (datetime.now() - saved).total_seconds()
        if age > 600:  # 10 minutos
            logger.warning("⚠️ Cache de sessão expirado (>10min)")
            cache_path.unlink(missing_ok=True)
            return None
        
        return cache
    except Exception as e:
        logger.error(f"❌ Erro ao carregar cache de sessão: {e}")
        return None


def _clear_session_cache():
    """Remove cache de sessão."""
    cache_path = TOKENS_DIR / SESSION_CACHE_FILE
    cache_path.unlink(missing_ok=True)


def validate_and_complete(
    session: Session, 
    csrf: str, 
    email: str, 
    otp: str, 
    account_name: str = "main"
) -> Optional[str]:
    """
    Completa a autenticação após usuário fornecer OTP.
    Usado em conjunto com send_verification_only().
    """
    try:
        redirect_url = validate_otp(session, email, otp)
        if not redirect_url:
            return None
        
        token = extract_session_token(session, redirect_url)
        if not token:
            return None
        
        save_token(token, account_name)
        update_env_file(token)
        
        return token
        
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return None


def check_current_token() -> bool:
    """Verifica se o token atual é válido."""
    try:
        from token_manager import TokenManager
        tm = TokenManager()
        
        if not tm.accounts:
            logger.warning("⚠️ Nenhum token configurado")
            return False
        
        logger.info("🔍 Verificando token atual...")
        is_valid = tm.validate_token()
        
        if is_valid:
            logger.info("✅ Token atual é válido!")
        else:
            logger.warning("⚠️ Token expirado ou inválido. Use refresh para renovar.")
        
        return is_valid
        
    except Exception as e:
        logger.error(f"❌ Erro ao verificar: {e}")
        return False


# ============= CLI =============

def main():
    parser = argparse.ArgumentParser(description='Renova token do Perplexity via email')
    parser.add_argument('--email', help='Email da conta Perplexity')
    parser.add_argument('--otp', help='Código OTP de 6 dígitos')
    parser.add_argument('--account', default='main', help='Nome da conta')
    parser.add_argument('--check', action='store_true', help='Apenas verifica token atual')
    parser.add_argument('--send-only', action='store_true', help='Apenas envia código (não valida)')
    
    args = parser.parse_args()
    
    if args.check:
        sys.exit(0 if check_current_token() else 1)
    
    if args.send_only and args.email:
        session, csrf = send_verification_only(args.email)
        if session and csrf:
            print("✅ Código enviado! Sessão salva em cache.")
            sys.exit(0)
        sys.exit(1)
    
    if args.email and args.otp:
        # Tenta carregar sessão do cache (fluxo 2 passos)
        cache = _load_session_cache()
        
        if cache and cache.get('email') == args.email:
            # Reutiliza sessão salva (não re-envia código)
            logger.info("🔄 Usando sessão cacheada...")
            session = Session(
                impersonate="chrome",
                headers={"Referer": BASE_URL, "Origin": BASE_URL}
            )
            # Restaura cookies
            for name, value in cache['cookies'].items():
                session.cookies.set(name, value)
            
            redirect_url = validate_otp(session, args.email, args.otp)
            if not redirect_url:
                _clear_session_cache()
                sys.exit(1)
            
            token = extract_session_token(session, redirect_url)
            _clear_session_cache()
            
            if not token:
                sys.exit(1)
            
            save_token(token, args.account)
            update_env_file(token)
            logger.info(f"✅ Token renovado! Preview: ...{token[-8:]}")
            sys.exit(0)
        else:
            # Sem cache: fluxo completo (re-envia código + valida)
            logger.warning("⚠️ Sem sessão cacheada, fazendo fluxo completo...")
            token = refresh_token_with_otp(args.email, args.otp, args.account)
            sys.exit(0 if token else 1)
    
    # Modo interativo
    token = refresh_token_interactive(args.account)
    sys.exit(0 if token else 1)


if __name__ == "__main__":
    main()
