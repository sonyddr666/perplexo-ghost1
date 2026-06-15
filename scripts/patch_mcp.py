"""
Apply token system rewrite patches to perplexity_mcp.py.
Reads original, removes old endpoints, inserts new ones.
"""
import pathlib

src = pathlib.Path('c:/Users/larri/Music/perplexo/perplexo-tapi/src/perplexity_mcp.py')
content = src.read_text(encoding='utf-8')
lines = content.split('\n')

print(f"Original: {len(lines)} lines")

# === PATCH 1: Update import and startup (around line 124-132) ===
old_import = '''# ============= TOKEN MANAGER =============
from token_manager import get_token_manager, TokenManager

try:
    token_manager = get_token_manager()
    logger.info(f"🔑 TokenManager inicializado: {len(token_manager.accounts)} conta(s)")
except Exception as e:
    logger.warning(f"⚠️ TokenManager não disponível: {e}")
    token_manager = None'''

new_import = '''# ============= TOKEN MANAGER =============
from token_manager import get_token_manager, TokenManager, _start_auto_refresh_worker, detect_input_type

try:
    token_manager = get_token_manager()
    logger.info(f"🔑 TokenManager v3: {token_manager.get_pool_status()['total']} token(s) no pool")
    # Inicia worker de auto-refresh (6h)
    _start_auto_refresh_worker(get_token_manager)
except Exception as e:
    logger.warning(f"⚠️ TokenManager não disponível: {e}")
    token_manager = None'''

content = content.replace(old_import, new_import)

# === PATCH 2: Find old endpoints section and replace ===
# Old endpoints start at "@app.route('/tokens', methods=['GET'])" 
# and end just before "# ============= CANVAS / FILE HELPERS ============="

marker_start = "@app.route('/tokens', methods=['GET'])\n@app.route('/tokens/status', methods=['GET'])"
marker_end = "# ============= CANVAS / FILE HELPERS ============="

idx_start = content.find(marker_start)
idx_end = content.find(marker_end)

if idx_start == -1:
    # Try with \r\n
    marker_start = marker_start.replace('\n', '\r\n')
    marker_end = marker_end
    idx_start = content.find(marker_start)
    idx_end = content.find(marker_end)

print(f"Start marker at: {idx_start}")
print(f"End marker at: {idx_end}")

if idx_start == -1 or idx_end == -1:
    print("ERROR: Could not find markers!")
    import sys
    sys.exit(1)

new_endpoints = '''@app.route('/tokens', methods=['GET'])
@app.route('/tokens/status', methods=['GET'])
@require_api_key
def tokens_status():
    """Status do pool + conta ativa (unificado v3)"""
    if token_manager is None:
        return jsonify({
            "active": False,
            "message": "TokenManager n\\u00e3o dispon\\u00edvel",
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
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    
    data = request.json or {}
    raw = data.get("input") or data.get("token") or data.get("cookie_string") or ""
    if not raw and data.get("cookies"):
        raw = json.dumps(data["cookies"])
    if not raw:
        return jsonify({"success": False, "message": "Campo 'input' obrigat\\u00f3rio"}), 400
    
    name = data.get("name")
    validate = data.get("validate", True)
    result = token_manager.set_token(str(raw), name=name, validate=validate)
    
    if result.get("success") and not result.get("duplicate"):
        current = token_manager.get_current_token()
        if current:
            client_manager.init_default(current)
    
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
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 400
    
    is_valid = token_manager.validate_token()
    pool_status = token_manager.get_pool_status()
    return jsonify({
        "valid": is_valid,
        "account": pool_status["current_account"],
        "pool_status": pool_status,
        "message": "Token v\\u00e1lido" if is_valid else "Token inv\\u00e1lido ou bloqueado",
        "token_preview": f"****{token_manager.get_current_token()[-8:]}" if token_manager.get_current_token() else None
    })


@app.route('/tokens/rotate', methods=['POST'])
@require_api_key
def tokens_rotate():
    """Rotaciona para o pr\\u00f3ximo token v\\u00e1lido"""
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    
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
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    
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
        results = token_manager.clear_all_tokens()
        client_manager.init_default(None)
        return jsonify({"status": "success", "message": "Todos os tokens apagados", "details": results})
    
    return jsonify({"error": f"A\\u00e7\\u00e3o desconhecida: {action}"}), 400


# === COMPAT: endpoints legados ===

@app.route('/tokens/refresh', methods=['POST', 'GET'])
@require_api_key
@limiter.limit("5 per minute")
def tokens_refresh_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
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
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    token_manager.reload_tokens()
    current = token_manager.get_current_token()
    if current:
        client_manager.init_default(current)
    return jsonify({"status": "ok", "details": token_manager.get_status()})

@app.route('/tokens/pool/status', methods=['GET'])
@require_api_key
def tokens_pool_status_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    return jsonify(token_manager.get_pool_status())

@app.route('/tokens/pool/smart_refresh', methods=['POST'])
@require_api_key
def tokens_pool_smart_refresh_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    results = token_manager.smart_refresh_all()
    current = token_manager.get_current_token()
    if current:
        client_manager.init_default(current)
    return jsonify(results)

@app.route('/tokens/pool/validate_all', methods=['POST'])
@require_api_key
def tokens_pool_validate_all_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    return jsonify(token_manager.validate_all_pool())

@app.route('/tokens/pool/clear_invalid', methods=['POST'])
@require_api_key
def tokens_pool_clear_invalid_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    removed = token_manager.clear_invalid()
    return jsonify({"removed": removed})

@app.route('/tokens/clear_all', methods=['POST'])
@require_api_key
def tokens_clear_all_compat():
    if not token_manager:
        return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503
    results = token_manager.clear_all_tokens()
    client_manager.init_default(None)
    return jsonify({"status": "success", "details": results})


'''

# Assemble: everything before old endpoints + new endpoints + everything from CANVAS onward
content = content[:idx_start] + new_endpoints + content[idx_end:]

# === PATCH 3: Update /config/token to use set_token ===
old_config = '''@app.route('/config/token', methods=['POST'])
@require_api_key
def config_token():
    """
    Atualiza o token de sess\\u00e3o do Perplexity em tempo de execu\\u00e7\\u00e3o.
    Payload: {"token": "seu_token_aqui"}
    """
    global PERPLEXITY_SESSION_TOKEN
    
    data = request.json or {}
    
    try:
        validated = TokenUpdateRequest(**data)
    except Exception as e:
        return jsonify({"error": f"Valida\\u00e7\\u00e3o falhou: {e}"}), 400
        
    try:
        # Reinicializa o cliente
        if SCRAPER_AVAILABLE and Perplexity:
            PERPLEXITY_SESSION_TOKEN = validated.token
            client_manager.init_default(validated.token)
            logger.info("\\u2705 Cliente Perplexity reinicializado com NOVO token!")
            return jsonify({"success": True, "message": "Token atualizado e cliente reinicializado!"})
        else:
            return jsonify({"error": "Scraper not available"}), 503
            
    except Exception as e:
        logger.error(f"Erro ao atualizar token: {e}")
        return jsonify({"error": str(e)}), 500'''

new_config = '''@app.route('/config/token', methods=['POST'])
@require_api_key
def config_token():
    """Compat: redireciona para /tokens/set"""
    data = request.json or {}
    token = data.get("token", "")
    if not token:
        return jsonify({"error": "Campo 'token' obrigat\\u00f3rio"}), 400
    if token_manager:
        result = token_manager.set_token(token, validate=False)
        if result.get("success"):
            client_manager.init_default(token_manager.get_current_token())
        return jsonify(result)
    return jsonify({"error": "TokenManager n\\u00e3o dispon\\u00edvel"}), 503'''

if old_config in content:
    content = content.replace(old_config, new_config)
    print("Patched /config/token")
else:
    print("WARNING: /config/token not found (may already be different)")

# Save
src.write_text(content, encoding='utf-8')
final_lines = content.split('\n')
print(f"Final: {len(final_lines)} lines")
print(f"token_manager references: {content.count('token_manager')}")
