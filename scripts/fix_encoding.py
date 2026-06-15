import pathlib

p = pathlib.Path('c:/Users/larri/Music/perplexo/perplexo-tapi/src/perplexity_mcp.py')
raw = p.read_bytes()

print(f"BOM: {raw[:3] == b'\\xef\\xbb\\xbf'}")
print(f"Has NUL in first 200: {b'\\x00' in raw[:200]}")
print(f"First 20 bytes hex: {raw[:20].hex()}")

# If UTF-16 LE (from PowerShell), convert to UTF-8
if b'\\x00' in raw[:200]:
    # Try UTF-16
    if raw[:2] == b'\\xff\\xfe':
        content = raw[2:].decode('utf-16-le', errors='replace')
    else:
        content = raw.decode('utf-16-le', errors='replace')
    print(f"Converted from UTF-16-LE")
elif raw[:3] == b'\\xef\\xbb\\xbf':
    content = raw[3:].decode('utf-8', errors='replace')
    print("Stripped UTF-8 BOM")
else:
    content = raw.decode('utf-8', errors='replace')
    print("Already UTF-8")

# Remove any NUL chars
content = content.replace('\\x00', '')

# Save clean UTF-8
p.write_text(content, encoding='utf-8')
count = content.count('token_manager')
print(f"Length: {len(content)}, token_manager found: {count} times")
