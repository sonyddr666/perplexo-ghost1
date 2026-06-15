#!/usr/bin/env python3
"""
Script de Extração de Cookies do Browser
=========================================
Extrai cookies do Chrome/Firefox logado no Perplexity.ai
e salva no formato esperado pelo TokenManager.

Uso:
    python scripts/extract_cookies.py --browser chrome --output data/tokens/cookies.json
    python scripts/extract_cookies.py --browser firefox --profile default
"""

import argparse
import json
import sqlite3
import shutil
import tempfile
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

try:
    # Para Chrome: precisa descriptografar cookies no Windows
    import win32crypt
    CRYPT_AVAILABLE = True
except ImportError:
    CRYPT_AVAILABLE = False
    print("⚠️ win32crypt não disponível - use modo manual")


def get_chrome_cookies_path() -> Optional[Path]:
    """Retorna caminho do arquivo de cookies do Chrome"""
    if os.name == 'nt':  # Windows
        local_app = Path(os.environ.get('LOCALAPPDATA', ''))
        return local_app / 'Google' / 'Chrome' / 'User Data' / 'Default' / 'Cookies'
    elif os.name == 'posix':  # Linux/Mac
        home = Path.home()
        chrome_path = home / '.config' / 'google-chrome' / 'Default' / 'Cookies'
        if chrome_path.exists():
            return chrome_path
        # Mac
        return home / 'Library' / 'Application Support' / 'Google' / 'Chrome' / 'Default' / 'Cookies'
    return None


def get_firefox_cookies_path(profile: str = 'default') -> Optional[Path]:
    """Retorna caminho do arquivo de cookies do Firefox"""
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA', '')) / 'Mozilla' / 'Firefox' / 'Profiles'
    else:
        base = Path.home() / '.mozilla' / 'firefox'
    
    if not base.exists():
        return None
    
    # Procura perfil
    for p in base.iterdir():
        if p.is_dir() and (profile in p.name or p.name.endswith('.default')):
            cookies_file = p / 'cookies.sqlite'
            if cookies_file.exists():
                return cookies_file
    return None


def extract_chrome_cookies(domain: str = '.perplexity.ai') -> Dict[str, str]:
    """Extrai cookies do Chrome para um domínio específico"""
    cookies_path = get_chrome_cookies_path()
    if not cookies_path or not cookies_path.exists():
        print(f"❌ Arquivo de cookies do Chrome não encontrado: {cookies_path}")
        return {}
    
    # Copia arquivo (pode estar bloqueado)
    temp_file = tempfile.mktemp(suffix='.db')
    shutil.copy2(cookies_path, temp_file)
    
    try:
        conn = sqlite3.connect(temp_file)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT name, value, encrypted_value 
            FROM cookies 
            WHERE host_key LIKE ?
        """, (f'%{domain}%',))
        
        cookies = {}
        for name, value, encrypted_value in cursor.fetchall():
            if value:
                cookies[name] = value
            elif encrypted_value and CRYPT_AVAILABLE:
                try:
                    decrypted = win32crypt.CryptUnprotectData(encrypted_value)[1].decode('utf-8')
                    cookies[name] = decrypted
                except Exception:
                    pass
        
        conn.close()
        return cookies
        
    finally:
        os.unlink(temp_file)


def extract_firefox_cookies(domain: str = '.perplexity.ai', profile: str = 'default') -> Dict[str, str]:
    """Extrai cookies do Firefox para um domínio específico"""
    cookies_path = get_firefox_cookies_path(profile)
    if not cookies_path or not cookies_path.exists():
        print(f"❌ Arquivo de cookies do Firefox não encontrado")
        return {}
    
    # Copia arquivo (pode estar bloqueado)
    temp_file = tempfile.mktemp(suffix='.db')
    shutil.copy2(cookies_path, temp_file)
    
    try:
        conn = sqlite3.connect(temp_file)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT name, value 
            FROM moz_cookies 
            WHERE host LIKE ?
        """, (f'%{domain}%',))
        
        cookies = {name: value for name, value in cursor.fetchall()}
        conn.close()
        return cookies
        
    finally:
        os.unlink(temp_file)


def save_to_token_file(cookies: Dict[str, str], output_path: Path, account_name: str = 'extracted'):
    """Salva cookies extraídos no formato do TokenManager"""
    session_token = cookies.get('__Secure-next-auth.session-token')
    
    if not session_token:
        print("❌ Token de sessão não encontrado nos cookies!")
        print(f"   Cookies encontrados: {list(cookies.keys())}")
        return False
    
    # Carrega arquivo existente ou cria novo
    if output_path.exists():
        with open(output_path, 'r') as f:
            data = json.load(f)
    else:
        data = {"accounts": [], "current_index": 0}
    
    # Remove conta existente com mesmo nome
    data['accounts'] = [a for a in data['accounts'] if a.get('name') != account_name]
    
    # Adiciona nova conta
    data['accounts'].append({
        "name": account_name,
        "session_token": session_token,
        "csrf_token": cookies.get('next-auth.csrf-token'),
        "extracted_at": datetime.now().isoformat(),
        "source": "browser_extraction"
    })
    
    data['updated_at'] = datetime.now().isoformat()
    
    # Salva
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Token extraído e salvo em: {output_path}")
    print(f"   Conta: {account_name}")
    print(f"   Token: {session_token[:30]}...{session_token[-10:]}")
    return True


def manual_input_mode(output_path: Path):
    """Modo de entrada manual de token"""
    print("\n🔧 MODO MANUAL - Cole seu token")
    print("=" * 50)
    print("1. Abra perplexity.ai no browser")
    print("2. Faça login na sua conta")
    print("3. Pressione F12 → Application → Cookies")
    print("4. Copie o valor de __Secure-next-auth.session-token")
    print("=" * 50)
    
    token = input("\nCole o token aqui: ").strip()
    if not token:
        print("❌ Token vazio!")
        return
    
    account_name = input("Nome da conta (default: 'manual'): ").strip() or 'manual'
    
    save_to_token_file(
        {'__Secure-next-auth.session-token': token},
        output_path,
        account_name
    )


def main():
    parser = argparse.ArgumentParser(description='Extrai cookies do browser para TokenManager')
    parser.add_argument('--browser', choices=['chrome', 'firefox', 'manual'], default='manual',
                        help='Browser para extrair (default: manual)')
    parser.add_argument('--profile', default='default',
                        help='Perfil do Firefox (default: default)')
    parser.add_argument('--output', type=Path, default=Path('data/tokens/cookies.json'),
                        help='Arquivo de saída')
    parser.add_argument('--account', default='extracted',
                        help='Nome da conta')
    
    args = parser.parse_args()
    
    if args.browser == 'manual':
        manual_input_mode(args.output)
    elif args.browser == 'chrome':
        print("🔍 Extraindo cookies do Chrome...")
        cookies = extract_chrome_cookies()
        if cookies:
            save_to_token_file(cookies, args.output, args.account)
    elif args.browser == 'firefox':
        print("🔍 Extraindo cookies do Firefox...")
        cookies = extract_firefox_cookies(profile=args.profile)
        if cookies:
            save_to_token_file(cookies, args.output, args.account)


if __name__ == '__main__':
    main()
