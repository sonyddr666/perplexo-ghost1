"""
Token Manager v3 — Pool Unificado
===================================
Fonte única de verdade: data/tokens/tokens_pool.json

Aceita 3 formatos de input:
  Tipo 1: JWT direto (eyJ...)
  Tipo 2: JSON Array [{name, value, ...}] (extensão do browser)
  Tipo 3: Cookie String (name=value; name2=value2)

Cookies complementares (cf_clearance, _bm, etc) ficam no root do pool,
compartilhados (são por IP/UA, não por conta).

O scraper NUNCA recebe cookies — só o JWT.
"""

import os
import json
import uuid
import logging
import threading
import time
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple

logger = logging.getLogger(__name__)

# ============= CONFIGURAÇÃO =============

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TOKENS_DIR = Path(os.getenv("TOKENS_DIR", str(BASE_DIR / "data" / "tokens")))
TOKEN_ROTATION_ENABLED = os.getenv("TOKEN_ROTATION_ENABLED", "true").lower() == "true"
POOL_FILE = "tokens_pool.json"

# Cookies complementares (root do pool, compartilhados entre contas)
COMPLEMENTARY_COOKIES = [
    "cf_clearance", "_bm", "__cflb", "__frs", "cf_bm",
    "next-auth.csrf-token", "__Secure-next-auth.callback-url"
]

# Cookies necessários do Perplexity (para compatibilidade)
PERPLEXITY_COOKIE_NAMES = [
    "__Secure-next-auth.session-token",
    "next-auth.csrf-token",
    "__Secure-next-auth.callback-url",
]

# Status possíveis
TOKEN_STATUS_VALID = "valid"
TOKEN_STATUS_INVALID = "invalid"
TOKEN_STATUS_CF_BLOCKED = "cf_blocked"
TOKEN_STATUS_UNKNOWN = "unknown"


def _public_credential_label(index: int) -> str:
    """Label seguro para respostas públicas, sem email/nome da conta."""
    return f"credencial_{index + 1}"


# ============= DETECÇÃO DE FORMATO =============

def detect_input_type(raw: str) -> str:
    """
    Detecta formato do input colado pelo usuário.
    Retorna: 'jwt' | 'cookies_array' | 'cookie_string' | 'unknown'
    """
    raw = raw.strip()
    if not raw:
        return "unknown"

    # Tipo 1: JWT direto
    if raw.startswith("eyJ") and len(raw) > 100 and "." in raw:
        return "jwt"

    # Tipo 2: JSON Array de cookies
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list) and any(
                isinstance(item, dict) and "name" in item and "value" in item
                for item in data[:5]
            ):
                return "cookies_array"
        except Exception:
            pass

    # Tipo 3: Cookie string (name=value; ...)
    if "=" in raw and (";" in raw or "__Secure" in raw or "cf_clearance" in raw):
        return "cookie_string"

    # Fallback: pode ser JWT sem prefixo padrão mas longo o bastante
    if len(raw) > 100 and "." in raw and " " not in raw[:20]:
        return "jwt"

    return "unknown"


def extract_jwt_from_input(raw: str) -> Tuple[Optional[str], dict]:
    """
    Extrai JWT + cookies complementares de qualquer formato de input.
    Retorna: (jwt_token | None, {cf_clearance: ..., _bm: ..., ...})
    """
    raw = raw.strip()
    input_type = detect_input_type(raw)
    complementary = {}
    jwt_token = None

    if input_type == "jwt":
        return raw, complementary

    all_cookies: Dict[str, str] = {}

    if input_type == "cookies_array":
        try:
            items = json.loads(raw)
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name", "")
                    value = item.get("value", "")
                    if name and value:
                        all_cookies[name] = value
        except Exception as e:
            logger.error(f"Erro ao parsear JSON array: {e}")
            return None, {}

    elif input_type == "cookie_string":
        all_cookies = parse_browser_cookies(raw)

    else:
        return None, {}

    jwt_token = all_cookies.get("__Secure-next-auth.session-token")

    for name in COMPLEMENTARY_COOKIES:
        val = all_cookies.get(name)
        if val:
            complementary[name] = val

    return jwt_token, complementary


# ============= PARSER DE COOKIES =============

def parse_browser_cookies(cookie_string: str) -> Dict[str, str]:
    """
    Parseia string de cookies do browser.
    Aceita:
      1. Formato Header (DevTools Network): name=value; name2=value2
      2. Formato Grid (DevTools Application): Name\tValue\tDomain...
    """
    cookies: Dict[str, str] = {}
    if not cookie_string:
        return cookies

    # Formato header (name=value; ...)
    if ";" in cookie_string or ("=" in cookie_string and "\n" not in cookie_string):
        for part in cookie_string.replace("\n", ";").split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    cookies[name] = value
        if cookies:
            return cookies

    # Formato Grid/Tabela
    for line in cookie_string.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "Name" in line and "Value" in line and "Domain" in line:
            continue  # cabeçalho
        parts = line.split("\t")
        if len(parts) >= 2:
            name = parts[0].strip()
            value = parts[1].strip()
            if name and 1 <= len(name) < 200:
                cookies[name] = value

    return cookies


def extract_session_token(cookie_string: str) -> Optional[str]:
    """Extrai apenas o session token de uma string de cookies."""
    cookies = parse_browser_cookies(cookie_string)
    return cookies.get("__Secure-next-auth.session-token")


# ============= TOKEN MANAGER v3 =============

class TokenManager:
    """
    Gerenciador unificado de tokens do Perplexity.ai v3.

    Fonte única de verdade: data/tokens/tokens_pool.json
    Estrutura:
    {
      "pool": [ {id, name, session_token, status, source, ...} ],
      "current_index": 0,
      "cookies": { "cf_clearance": "...", "_bm": "..." },
      "cookies_updated_at": "...",
      "cookies_status": "ok|expired|unknown"
    }
    """

    def __init__(self, tokens_dir: Path = None, cookies_file: str = None):
        self.tokens_dir = Path(tokens_dir or DEFAULT_TOKENS_DIR)
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self._env_token = os.getenv("PERPLEXITY_SESSION_TOKEN", "")
        self._lock = threading.Lock()

        # Compat: cookies_file param ignorado (legado)
        self.cookies_file = cookies_file or "cookies.json"

        # Migra arquivos legados
        self._migrate_legacy_files()

        # Fallback: env token → pool
        pool = self._load_pool()
        if not pool["pool"] and self._env_token:
            self.set_token(self._env_token, source="environment", validate=False)
            logger.info("📌 Token do .env adicionado ao pool")

        self._log_pool_summary()

        # Compat: propriedades legadas calculadas do pool
        self._sync_legacy_properties()

    # ============= COMPATIBILIDADE LEGADA =============

    def _sync_legacy_properties(self):
        """Sincroniza propriedades legadas (accounts, current_index) a partir do pool."""
        pool = self._load_pool()
        self.accounts = []
        for entry in pool["pool"]:
            self.accounts.append({
                "name": _public_credential_label(pool["pool"].index(entry)) if entry else "credencial",
                "email": entry.get("email"),
                "session_token": entry.get("session_token", ""),
                "source": entry.get("source", "pool"),
                "pool_id": entry.get("id"),
                "status": entry.get("status", TOKEN_STATUS_UNKNOWN),
            })
        self.current_index = pool.get("current_index", 0)

    # ============= MIGRAÇÃO LEGADA =============

    def _migrate_legacy_files(self):
        """
        Na primeira execução com v3: importa cookies.json + browser_cookies.json
        para o pool unificado e renomeia para .bak
        """
        pool = self._load_pool()

        # Só migra se pool está vazio (idempotente)
        if pool["pool"]:
            return

        cookies_path = self.tokens_dir / self.cookies_file
        raw_path = self.tokens_dir / "browser_cookies.json"
        migrated = False

        # 1. Importa tokens de cookies.json
        if cookies_path.exists():
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                accounts = data.get("accounts", [])
                count = 0
                for acc in accounts:
                    token = acc.get("session_token", "")
                    if token and token.startswith("eyJ"):
                        self._add_jwt_to_pool(
                            token,
                            name=acc.get("name") or acc.get("email"),
                            source="migrated_cookies_json",
                            validate=False
                        )
                        count += 1
                cookies_path.rename(cookies_path.with_suffix(".json.bak"))
                logger.info(f"✅ Migração: {count} token(s) de cookies.json → pool")
                migrated = True
            except Exception as e:
                logger.error(f"Erro ao migrar cookies.json: {e}")

        # 2. Importa cookies complementares de browser_cookies.json
        if raw_path.exists():
            try:
                with open(raw_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                if isinstance(raw_data, list):
                    pool = self._load_pool()
                    for c in raw_data:
                        if not isinstance(c, dict):
                            continue
                        name = c.get("name", "")
                        value = c.get("value", "")
                        if name in COMPLEMENTARY_COOKIES and value:
                            pool["cookies"][name] = value
                        # JWT também
                        if name == "__Secure-next-auth.session-token" and value.startswith("eyJ"):
                            self._add_jwt_to_pool(
                                value,
                                source="migrated_browser_cookies",
                                validate=False
                            )
                    pool["cookies_updated_at"] = datetime.now().isoformat()
                    pool["cookies_status"] = "unknown"
                    self._save_pool(pool)
                raw_path.rename(raw_path.with_suffix(".json.bak"))
                logger.info("✅ Migração: cookies complementares de browser_cookies.json → pool.cookies")
                migrated = True
            except Exception as e:
                logger.error(f"Erro ao migrar browser_cookies.json: {e}")

        if migrated:
            logger.info("🔄 Migração legada concluída. Arquivos renomeados para .bak")

    # ============= POOL I/O =============

    def _load_pool(self) -> dict:
        """Carrega pool do disco."""
        pool_path = self.tokens_dir / POOL_FILE
        if pool_path.exists():
            try:
                with open(pool_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("pool", [])
                data.setdefault("cookies", {})
                data.setdefault("current_index", 0)
                data.setdefault("cookies_updated_at", None)
                data.setdefault("cookies_status", "unknown")
                return data
            except Exception as e:
                logger.error(f"Erro ao ler {POOL_FILE}: {e}")
        return {
            "pool": [],
            "current_index": 0,
            "cookies": {},
            "cookies_updated_at": None,
            "cookies_status": "unknown"
        }

    def _save_pool(self, pool_data: dict):
        """Salva pool no disco com escrita atômica (tmp → rename)."""
        pool_path = self.tokens_dir / POOL_FILE
        pool_data["updated_at"] = datetime.now().isoformat()
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.tokens_dir), suffix=".tmp", prefix="pool_"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(pool_data, f, indent=2, ensure_ascii=False)
            # Atômico no mesmo filesystem
            os.replace(tmp_path, str(pool_path))
        except Exception as e:
            logger.error(f"Erro ao salvar {POOL_FILE}: {e}")
            # Cleanup temp
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        # Sync compat
        self._sync_legacy_properties()

    def _log_pool_summary(self):
        """Log do estado do pool."""
        s = self.get_pool_status()
        logger.info(
            f"🔑 Pool: {s['total']} token(s) — "
            f"✅{s['valid']} 🟠{s['cf_blocked']} {s['invalid']} {s['unknown']}"
        )

    # ============= ENTRADA UNIFICADA =============

    def set_token(self, raw_input: str, name: str = None,
                  source: str = "manual", validate: bool = True) -> dict:
        """
        Endpoint unificado de entrada — aceita JWT, JSON Array, ou Cookie String.
        Detecta formato, extrai JWT, salva cookies complementares no root.

        Retorna: {success, token_id, name, status, input_type, message, duplicate}
        """
        with self._lock:
            input_type = detect_input_type(raw_input)

            if input_type == "unknown":
                return {
                    "success": False,
                    "token_id": None,
                    "name": name or "desconhecido",
                    "status": "error",
                    "input_type": "unknown",
                    "message": " Formato não reconhecido. Cole JWT (eyJ...), JSON array de cookies, ou cookie string.",
                    "duplicate": False
                }

            jwt_token, complementary = extract_jwt_from_input(raw_input)

            if not jwt_token:
                return {
                    "success": False,
                    "token_id": None,
                    "name": name or "desconhecido",
                    "status": "error",
                    "input_type": input_type,
                    "message": " JWT (__Secure-next-auth.session-token) não encontrado no input.",
                    "duplicate": False
                }

            # Salva cookies complementares no root do pool
            if complementary:
                pool = self._load_pool()
                pool["cookies"].update(complementary)
                pool["cookies_updated_at"] = datetime.now().isoformat()
                pool["cookies_status"] = "ok"
                self._save_pool(pool)
                logger.info(f" Cookies complementares atualizados: {list(complementary.keys())}")

            # Adiciona JWT ao pool
            result = self._add_jwt_to_pool(jwt_token, name=name, source=source, validate=validate)
            result["input_type"] = input_type
            return result

    def _add_jwt_to_pool(self, jwt: str, name: str = None,
                         source: str = "manual", validate: bool = True) -> dict:
        """Adiciona JWT ao pool. Verifica duplicatas. Probe real se validate=True."""
        pool = self._load_pool()

        # Verifica duplicata
        for entry in pool["pool"]:
            if entry.get("session_token") == jwt:
                # Atualiza status se pedido
                if validate:
                    status, reason = self._probe_endpoint(jwt)
                    entry["status"] = status
                    entry["last_validated"] = datetime.now().isoformat()
                    entry["invalidation_reason"] = None if status == "valid" else reason
                    self._save_pool(pool)
                logger.info(f"⚠ Token duplicado: {entry['id']}")
                return {
                    "success": True,
                    "token_id": entry["id"],
                    "name": _public_credential_label(pool["pool"].index(entry)),
                    "status": entry.get("status", "unknown"),
                    "message": f"ℹ Token já existe: '{entry.get('name')}' ({entry.get('status')})",
                    "duplicate": True
                }

        token_id = f"tk_{uuid.uuid4().hex[:6]}"
        status = TOKEN_STATUS_UNKNOWN

        # Tenta enriquecer com email real
        email = None
        if not name or name in ("auto_refreshed", "main", "conta_0", "ENV_TOKEN"):
            user_info = self.fetch_user_info(jwt)
            if user_info.get("email"):
                email = user_info["email"]
                name = email
            elif user_info.get("name"):
                name = user_info["name"]

        # Probe real
        if validate:
            status, reason = self._probe_endpoint(jwt)
            logger.info(f"🩺 Probe '{name or token_id}': {status} — {reason}")

        entry = {
            "id": token_id,
            "name": name or f"token_{len(pool['pool']) + 1}",
            "email": email,
            "session_token": jwt,
            "status": status,
            "source": source,
            "added_at": datetime.now().isoformat(),
            "last_used": None,
            "last_validated": datetime.now().isoformat() if validate else None,
            "invalidation_reason": None
        }

        pool["pool"].append(entry)
        self._save_pool(pool)
        logger.info(f"✅ Token adicionado: {token_id} ({entry['name']}) status={status}")

        return {
            "success": True,
            "token_id": token_id,
            "name": _public_credential_label(len(pool["pool"]) - 1),
            "status": status,
            "message": f"✅ Token '{entry['name']}' adicionado (status: {status})",
            "duplicate": False
        }

    # ============= VALIDAÇÃO COM PROBE REAL =============

    def _probe_endpoint(self, token: str) -> Tuple[str, str]:
        """
        Probe duplo contra /api/auth/session:
        1) COM cookies complementares → testa JWT + Cloudflare juntos
        2) Se 403 → testa SEM cookies → distingue JWT morto de CF bloqueando

        Retorna: (status, reason)
        """
        pool = self._load_pool()
        complementary = pool.get("cookies", {})

        headers = {
            "Accept": "*/*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
            "Referer": "https://www.perplexity.ai/",
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-app-apiclient": "default",
            "x-app-apiversion": "2.18",
        }

        url = "https://www.perplexity.ai/api/auth/session?version=2.18&source=default"

        # Probe a: COM cookies (teste completo)
        all_cookies = {"__Secure-next-auth.session-token": token, **complementary}
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.get(
                url, headers=headers, cookies=all_cookies,
                impersonate="chrome", timeout=12
            )
        except ImportError:
            # Fallback sem curl_cffi
            import requests
            try:
                cookie_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
                resp = requests.get(
                    url,
                    headers={**headers, "Cookie": cookie_str},
                    timeout=12
                )
            except Exception as e:
                return "error", f"Requisição falhou: {str(e)[:100]}"
        except Exception as e:
            return "error", f"Exceção: {str(e)[:100]}"

        if resp.status_code == 200:
            try:
                data = resp.json()
                if data.get("user"):
                    self._capture_response_cookies(resp)
                    return "valid", "OK"
                return "invalid", "Sessão sem user — JWT expirado"
            except Exception:
                return "error", "Falha ao parsear resposta JSON"

        if resp.status_code == 403:
            # Probe b: marca cookies como expirados
            pool["cookies_status"] = "expired"
            self._save_pool(pool)
            return "cf_blocked", "Cloudflare bloqueou — cookies complementares expirados ou IP banido"

        if resp.status_code == 401:
            return "invalid", "401 Unauthorized — JWT revogado ou expirado"

        return "error", f"Status inesperado: {resp.status_code}"

    def _capture_response_cookies(self, response):
        """Captura cookies complementares da resposta HTTP e atualiza root do pool."""
        try:
            if not hasattr(response, 'cookies'):
                return
            pool = self._load_pool()
            updated = False
            for name in COMPLEMENTARY_COOKIES:
                val = response.cookies.get(name)
                if val and pool["cookies"].get(name) != val:
                    pool["cookies"][name] = val
                    updated = True
                    logger.debug(f" Cookie atualizado: {name}")
            if updated:
                pool["cookies_updated_at"] = datetime.now().isoformat()
                pool["cookies_status"] = "ok"
                self._save_pool(pool)
        except Exception as e:
            logger.debug(f"Aviso ao capturar cookies: {e}")

    def validate_token(self, token: str = None) -> bool:
        """Valida token via probe real. Retorna True/False."""
        if not token:
            token = self.get_current_token()
        if not token:
            return False
        with self._lock:
            status, reason = self._probe_endpoint(token)
            self._update_token_status_by_value(token, status, reason)
            return status == "valid"

    def _update_token_status_by_value(self, token: str, new_status: str, reason: str = None):
        """Atualiza status de um token no pool pelo valor do JWT."""
        pool = self._load_pool()
        for entry in pool["pool"]:
            if entry.get("session_token") == token:
                entry["status"] = new_status
                entry["last_validated"] = datetime.now().isoformat()
                entry["invalidation_reason"] = None if new_status == "valid" else reason
                break
        self._save_pool(pool)

    def get_token_id(self, token: str) -> Optional[str]:
        """Resolve o ID do pool a partir do valor do JWT."""
        if not token:
            return None
        pool = self._load_pool()
        for entry in pool["pool"]:
            if entry.get("session_token") == token:
                return entry.get("id")
        return None

    # ============= REFRESH =============

    def refresh_token(self, token_id: str = None) -> dict:
        """
        Tenta renovar um token usando os cookies complementares do pool root.
        Atualiza entry existente (não cria auto_refreshed).
        """
        with self._lock:
            pool = self._load_pool()
            complementary = pool.get("cookies", {})

            if not complementary:
                return {
                    "success": False,
                    "new_token": None,
                    "message": " Sem cookies complementares. Cole JSON array de cookies primeiro."
                }

            # Determina token alvo
            target_entry = None
            target_token = None

            if token_id:
                target_entry = next((e for e in pool["pool"] if e["id"] == token_id), None)
                if target_entry:
                    target_token = target_entry["session_token"]

            if not target_token:
                valid = [e for e in pool["pool"] if e.get("status") == "valid"]
                if valid:
                    idx = pool.get("current_index", 0) % len(valid)
                    target_entry = valid[idx]
                    target_token = target_entry["session_token"]
                elif pool["pool"]:
                    target_entry = pool["pool"][0]
                    target_token = target_entry["session_token"]

            if not target_token:
                return {"success": False, "new_token": None, "message": " Nenhum token para refresh"}

            all_cookies = {**complementary, "__Secure-next-auth.session-token": target_token}

            headers = {
                "Accept": "*/*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
                "Referer": "https://www.perplexity.ai/",
                "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "x-app-apiclient": "default",
                "x-app-apiversion": "2.18",
            }

            try:
                from curl_cffi import requests as cffi_requests
                resp = cffi_requests.get(
                    "https://www.perplexity.ai/api/auth/session?version=2.18&source=default",
                    headers=headers, cookies=all_cookies,
                    impersonate="chrome", timeout=15
                )
            except ImportError:
                return {"success": False, "new_token": None, "message": " curl_cffi não instalado"}
            except Exception as e:
                return {"success": False, "new_token": None, "message": f" Erro: {e}"}

            if resp.status_code == 403:
                pool["cookies_status"] = "expired"
                self._save_pool(pool)
                return {"success": False, "new_token": None,
                        "message": " Cloudflare bloqueou (403). Cookies complementares expirados."}

            if resp.status_code == 401:
                if target_entry:
                    target_entry["status"] = TOKEN_STATUS_INVALID
                    target_entry["invalidation_reason"] = "401 no refresh"
                    self._save_pool(pool)
                return {"success": False, "new_token": None, "message": " Token expirado (401)"}

            if resp.status_code != 200:
                return {"success": False, "new_token": None,
                        "message": f" Status inesperado: {resp.status_code}"}

            # Captura cookies da resposta
            self._capture_response_cookies(resp)

            # Verifica novo session token
            new_token = resp.cookies.get("__Secure-next-auth.session-token") if hasattr(resp, 'cookies') else None

            if new_token and new_token != target_token:
                if target_entry:
                    target_entry["session_token"] = new_token
                    target_entry["status"] = TOKEN_STATUS_VALID
                    target_entry["last_validated"] = datetime.now().isoformat()
                    target_entry["source"] = "browser_refresh"
                    target_entry["invalidation_reason"] = None
                    self._save_pool(pool)
                else:
                    self._add_jwt_to_pool(new_token, source="browser_refresh", validate=False)
                logger.info(f"🎉 Token renovado: ****{new_token[-8:]}")
                return {"success": True, "new_token": new_token,
                        "message": "✅ Token renovado com sucesso!"}

            if target_entry:
                target_entry["status"] = TOKEN_STATUS_VALID
                target_entry["last_validated"] = datetime.now().isoformat()
                target_entry["invalidation_reason"] = None
                self._save_pool(pool)

            return {"success": True, "new_token": None,
                    "message": "ℹ Token ainda válido (servidor não devolveu novo JWT)"}

    def refresh_from_browser_cookies(self) -> dict:
        """Alias de compatibilidade → refresh_token()"""
        result = self.refresh_token()
        # Formato esperado pelo código legado
        return {
            "success": result.get("success", False),
            "new_token": result.get("new_token"),
            "old_token": None,
            "message": result.get("message", "")
        }

    def smart_refresh_all(self) -> dict:
        """Tenta refresh em todos os tokens válidos + cf_blocked do pool."""
        pool = self._load_pool()
        results = {
            "new_tokens": 0, "still_valid": 0, "now_invalid": 0,
            "details": {"new_tokens": [], "still_valid": [], "now_invalid": []}
        }

        for entry in pool["pool"]:
            if entry.get("status") not in (TOKEN_STATUS_VALID, TOKEN_STATUS_CF_BLOCKED, TOKEN_STATUS_UNKNOWN):
                continue

            result = self.refresh_token(token_id=entry["id"])
            safe_name = _public_credential_label(pool["pool"].index(entry))

            if result.get("new_token"):
                results["new_tokens"] += 1
                results["details"]["new_tokens"].append({
                    "source": safe_name,
                    "new_id": entry["id"],
                    "preview": "redacted"
                })
            elif result.get("success"):
                results["still_valid"] += 1
                results["details"]["still_valid"].append(safe_name)
            else:
                results["now_invalid"] += 1
                results["details"]["now_invalid"].append(safe_name)

        return results

    # ============= FEEDBACK LOOP =============

    def mark_failed(self, token: str = None, token_id: str = None,
                    reason: str = "cf_blocked"):
        """Marca token como falho após erro real no scraper."""
        with self._lock:
            pool = self._load_pool()
            for entry in pool["pool"]:
                match = (token_id and entry["id"] == token_id) or \
                        (token and entry.get("session_token") == token)
                if match:
                    entry["status"] = reason
                    entry["invalidation_reason"] = reason
                    entry["last_validated"] = datetime.now().isoformat()
                    emoji = "🚫" if reason == "cf_blocked" else ""
                    logger.warning(f"{emoji} Token {entry['id']} ({entry.get('name')}) → {reason}")
                    break
            self._save_pool(pool)

    def mark_invalid(self, token_id: str = None, token: str = None,
                     reason: str = "session_expired"):
        """Marca token como inválido. Compatível com razões legacy."""
        status = TOKEN_STATUS_CF_BLOCKED if reason == "cf_blocked" else TOKEN_STATUS_INVALID
        self.mark_failed(token=token, token_id=token_id, reason=status)

    def mark_cf_blocked(self, token_id: str = None, token: str = None):
        """Atalho para marcar como bloqueado pelo Cloudflare."""
        self.mark_failed(token=token, token_id=token_id, reason=TOKEN_STATUS_CF_BLOCKED)

    # ============= ROTAÇÃO =============

    def get_current_token(self) -> Optional[str]:
        """Retorna o token ativo atual (sem rotacionar)."""
        pool = self._load_pool()
        valid = [e for e in pool["pool"] if e.get("status") in (TOKEN_STATUS_VALID, TOKEN_STATUS_UNKNOWN)]
        if not valid:
            # Fallback: qualquer token (incluindo cf_blocked)
            if pool["pool"]:
                return pool["pool"][0]["session_token"]
            return self._env_token or None
        idx = pool.get("current_index", 0) % len(valid)
        return valid[idx]["session_token"]

    def get_next_token(self) -> Optional[str]:
        """Retorna próximo token (rotação round-robin). Compat legado."""
        token, _ = self.get_next_valid_token()
        return token

    def get_next_valid_token(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Retorna (session_token, token_id) do próximo token válido.
        Pula cf_blocked e invalid.
        """
        with self._lock:
            pool = self._load_pool()
            valid = [e for e in pool["pool"]
                     if e.get("status") in (TOKEN_STATUS_VALID, TOKEN_STATUS_UNKNOWN)]

            if not valid:
                if self._env_token:
                    return self._env_token, None
                return None, None

            idx = pool.get("current_index", 0) % len(valid)
            entry = valid[idx]
            pool["current_index"] = (idx + 1) % len(valid)
            entry["last_used"] = datetime.now().isoformat()
            self._save_pool(pool)
            return entry["session_token"], entry["id"]

    def rotate(self) -> dict:
        """Avança para o próximo token válido. Retorna info."""
        token, token_id = self.get_next_valid_token()
        if not token:
            return {"success": False, "message": " Nenhum token válido disponível",
                    "token": None, "account": {}}
        pool = self._load_pool()
        entry = next((e for e in pool["pool"] if e["id"] == token_id), {})
        return {
            "success": True,
            "token_id": token_id,
            "token": token,
            "name": _public_credential_label(pool["pool"].index(entry)) if entry else "credencial",
            "message": "Credencial rotacionada",
            "account": self._public_token_entry(entry, pool["pool"].index(entry)) if entry else {}
        }

    # ============= POOL MANAGEMENT =============

    def validate_all_pool(self) -> dict:
        """Valida todos os tokens com probe real."""
        pool = self._load_pool()
        results = {"valid": 0, "invalid": 0, "cf_blocked": 0, "error": 0}

        for entry in pool["pool"]:
            status, reason = self._probe_endpoint(entry["session_token"])
            entry["status"] = status
            entry["last_validated"] = datetime.now().isoformat()
            entry["invalidation_reason"] = None if status == "valid" else reason
            key = status if status in results else "error"
            results[key] += 1

        self._save_pool(pool)
        return results

    def clear_invalid(self) -> int:
        """Remove tokens inválidos do pool."""
        with self._lock:
            pool = self._load_pool()
            before = len(pool["pool"])
            pool["pool"] = [e for e in pool["pool"]
                            if e.get("status") not in (TOKEN_STATUS_INVALID, "session_invalid")]
            pool["current_index"] = 0
            self._save_pool(pool)
            removed = before - len(pool["pool"])
            if removed:
                logger.info(f"🗑 {removed} token(s) inválido(s) removidos")
            return removed

    def get_pool_status(self) -> dict:
        """Status completo do pool incluindo cf_blocked."""
        pool = self._load_pool()
        entries = pool["pool"]

        def count(s):
            return len([e for e in entries if e.get("status") == s])

        valid_entries = [e for e in entries if e.get("status") == TOKEN_STATUS_VALID]
        current_entry = None
        if valid_entries:
            idx = pool.get("current_index", 0) % len(valid_entries)
            current_entry = valid_entries[idx]
        elif entries:
            current_entry = entries[0]

        current_idx = entries.index(current_entry) if current_entry in entries else -1

        return {
            "total": len(entries),
            "valid": count(TOKEN_STATUS_VALID),
            "cf_blocked": count(TOKEN_STATUS_CF_BLOCKED),
            "invalid": count(TOKEN_STATUS_INVALID) + count("session_invalid"),
            "unknown": count(TOKEN_STATUS_UNKNOWN),
            "active": bool(valid_entries),
            "current_account": {
                "id": current_entry["id"] if current_entry else None,
                "name": _public_credential_label(current_idx) if current_entry else "N/A",
                "email": None,
                "last_validated": current_entry.get("last_validated") if current_entry else None,
            } if current_entry else {"id": None, "name": "N/A", "email": None, "last_validated": None},
            "has_complementary_cookies": bool(pool.get("cookies")),
            "complementary_cookies_keys": list(pool.get("cookies", {}).keys()),
            "cookies_status": pool.get("cookies_status", "unknown"),
            "cookies_updated_at": pool.get("cookies_updated_at"),
            "tokens": [self._public_token_entry(e, idx) for idx, e in enumerate(entries)]
        }

    def _public_token_entry(self, entry: dict, index: int) -> dict:
        """Representacao segura de uma credencial para APIs externas."""
        return {
            "id": entry.get("id"),
            "name": _public_credential_label(index),
            "email": None,
            "status": entry.get("status", TOKEN_STATUS_UNKNOWN),
            "preview": "redacted",
            "source": entry.get("source", "?"),
            "last_validated": entry.get("last_validated"),
            "last_used": entry.get("last_used"),
            "invalidation_reason": entry.get("invalidation_reason"),
        }

    def get_complementary_cookies(self) -> dict:
        """Retorna cookies complementares do root do pool."""
        pool = self._load_pool()
        return pool.get("cookies", {})

    # ============= INFO =============

    def fetch_user_info(self, token: str) -> dict:
        """Busca info do usuário (email/nome) na API do Perplexity."""
        try:
            import requests
            resp = requests.get(
                "https://www.perplexity.ai/api/auth/session",
                headers={
                    "Cookie": f"__Secure-next-auth.session-token={token}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                },
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                if "user" in data:
                    return data["user"]
        except Exception as e:
            logger.debug(f"fetch_user_info: {e}")
        return {}

    def get_account_info(self) -> dict:
        """Retorna info da conta ativa. Compat legado."""
        status = self.get_pool_status()
        current = status["current_account"]
        return {
            "name": current.get("name", "N/A"),
            "email": current.get("email"),
            "last_validated": current.get("last_validated"),
        }

    def get_status(self) -> dict:
        """Status formatado para endpoints legados."""
        status = self.get_pool_status()
        return {
            "active": status["active"],
            "total_accounts": status["total"],
            "current_index": 0,
            "current_account": status["current_account"],
            "rotation_enabled": TOKEN_ROTATION_ENABLED,
            "tokens_dir": str(self.tokens_dir),
        }

    # ============= COMPATIBILIDADE LEGADA =============

    def reload_tokens(self):
        """Recarrega do disco. Compat legado."""
        self._sync_legacy_properties()

    def reload(self):
        """Alias para reload_tokens."""
        self.reload_tokens()

    def add_account(self, name: str, session_token: str, validate: bool = True) -> bool:
        """Compat legado → set_token."""
        result = self.set_token(session_token, name=name, validate=validate)
        return result.get("success", False)

    def remove_account(self, name: str):
        """Remove conta por nome."""
        with self._lock:
            pool = self._load_pool()
            pool["pool"] = [e for e in pool["pool"] if e.get("name") != name]
            self._save_pool(pool)

    def add_to_pool(self, token: str, name: str = None,
                    validate: bool = True, source_id: str = None) -> dict:
        """Compat legado → set_token."""
        result = self.set_token(token, name=name, validate=validate, source="legacy_add_to_pool")
        result["id"] = result.get("token_id")
        return result

    def _save_refreshed_token(self, token: str):
        """Compat legado — redireciona para set_token."""
        self.set_token(token, source="browser_refresh", validate=False)

    def _save_state(self):
        """Compat legado — não faz nada, pool já é salvo automaticamente."""
        pass

    def _backup_clearance(self):
        """Backup dos cookies complementares."""
        pool = self._load_pool()
        cookies = pool.get("cookies", {})
        if cookies:
            backup_path = self.tokens_dir / ".clearance_backup.json"
            try:
                with open(backup_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, indent=2)
                logger.info("💾 Backup de cookies complementares salvo")
            except Exception as e:
                logger.error(f"Erro ao salvar backup: {e}")

    def get_clearance_backup(self) -> list:
        """Restaura backup de cookies complementares."""
        backup_path = self.tokens_dir / ".clearance_backup.json"
        if backup_path.exists():
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return [{"name": k, "value": v, "domain": ".perplexity.ai"} for k, v in data.items()]
                return data if isinstance(data, list) else []
            except Exception:
                pass
        return []

    def clear_all_tokens(self) -> dict:
        """Apaga TODOS os tokens e reseta o sistema. Preserva backup de cookies."""
        self._backup_clearance()

        result = {"status": "success", "details": {"files_deleted": [], "runtime_env_cleared": True}}

        files_to_delete = [
            POOL_FILE,
            "cookies.json",
            "browser_cookies.json",
            ".refresh_session_cache.json",
        ]

        for fname in files_to_delete:
            fpath = self.tokens_dir / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                    result["details"]["files_deleted"].append(fname)
                except Exception as e:
                    logger.error(f"Erro ao deletar {fname}: {e}")

        self.accounts = []
        self.current_index = 0
        self._env_token = ""
        logger.warning("⚠ TODOS os tokens foram apagados!")
        return result


# ============= AUTO-REFRESH WORKER =============

def _start_auto_refresh_worker(token_manager_factory):
    """
    Background thread: a cada 6h verifica se pool degradado.
    Se valid < total//2, dispara smart_refresh_all().
    """
    def _loop():
        time.sleep(300)  # 5 min warmup
        while True:
            try:
                tm = token_manager_factory()
                if tm:
                    status = tm.get_pool_status()
                    total = status["total"]
                    valid = status["valid"]
                    if total > 0 and valid < max(1, total // 2):
                        logger.warning(f"🔄 Auto-refresh: {valid}/{total} válidos. Renovando...")
                        results = tm.smart_refresh_all()
                        logger.info(f"✅ Auto-refresh: novos={results['new_tokens']} válidos={results['still_valid']} inválidos={results['now_invalid']}")
                    else:
                        logger.info(f"🔋 Auto-refresh check: {valid}/{total} OK")
            except Exception as e:
                logger.error(f"Erro no auto-refresh: {e}")
            time.sleep(6 * 3600)  # 6 horas

    t = threading.Thread(target=_loop, daemon=True, name="token-auto-refresh")
    t.start()
    logger.info(" Auto-refresh worker iniciado (6h)")
    return t


# ============= FUNÇÕES UTILITRIAS =============

def create_cookies_file_template(output_path: Path = None) -> Path:
    """Cria template vazio (compat legado)."""
    if output_path is None:
        output_path = DEFAULT_TOKENS_DIR / "tokens_pool.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists():
        template = {
            "pool": [],
            "current_index": 0,
            "cookies": {},
            "cookies_updated_at": None,
            "cookies_status": "unknown"
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)
    return output_path


# ============= SINGLETON =============

_token_manager: Optional[TokenManager] = None


def get_token_manager() -> TokenManager:
    """Retorna instância singleton do TokenManager."""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
    return _token_manager


# ============= CLI =============

if __name__ == "__main__":
    import sys
    import pprint

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    tm = TokenManager()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            pprint.pprint(tm.get_pool_status())
        elif cmd == "validate":
            token = sys.argv[2] if len(sys.argv) > 2 else None
            print(f"Válido: {tm.validate_token(token)}")
        elif cmd == "set" and len(sys.argv) >= 3:
            raw = " ".join(sys.argv[2:])
            result = tm.set_token(raw)
            print(result["message"])
        elif cmd == "refresh":
            result = tm.refresh_token()
            print(result["message"])
        elif cmd == "refresh-all":
            pprint.pprint(tm.smart_refresh_all())
        else:
            print("Uso: python token_manager.py status|validate|set <input>|refresh|refresh-all")
    else:
        s = tm.get_pool_status()
        print(f"\n🔑 Pool: {s['total']} token(s) — ✅{s['valid']} 🟠{s['cf_blocked']} {s['invalid']} {s['unknown']}")
