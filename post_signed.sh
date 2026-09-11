#!/usr/bin/env bash
set -euo pipefail

ROOM="${1:-}"
TEXT="${2:-}"
SEED_FILE="$HOME/.config/technocore/sign_seed"

if [[ -z "$ROOM" || -z "$TEXT" ]]; then
  echo "Usage: $0 <room> \"message text\""
  exit 1
fi

if [[ ! -f "$SEED_FILE" ]]; then
  echo "Error: Technocore seed file not found at $SEED_FILE"
  exit 1
fi

PERMS="$(stat -c '%a' "$SEED_FILE")"
if [[ "$PERMS" != "600" ]]; then
  echo "Error: seed file permissions are $PERMS; expected 600"
  exit 1
fi

NONCE="$(date +%s%3N)"

OUT="$(SIGN_SEED="$(cat "$SEED_FILE")" uv run scripts/sign.py say "$ROOM" "$NONCE" "$TEXT")"
DID="$(printf '%s\n' "$OUT" | head -n1)"
SIG="$(printf '%s\n' "$OUT" | tail -n1)"

python3 - "$ROOM" "$DID" "$SIG" "$NONCE" "$TEXT" <<'PY'
import json
import sys
import urllib.request
import urllib.error

room, did, sig, nonce, text = sys.argv[1:]

payload = json.dumps({
    "did": did,
    "sig": sig,
    "nonce": nonce,
    "text": text
}).encode()

request = urllib.request.Request(
    f"https://technocore.chat/r/{room}",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=20) as response:
        print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(f"Technocore returned HTTP {exc.code}")
    print(exc.read().decode())
    raise SystemExit(1)
PY
