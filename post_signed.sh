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

DID="$(SIGN_SEED="$(cat "$SEED_FILE")" uv run scripts/sign.py did)"

NONCE="$(python3 - "$DID" "$ROOM" <<'PYNONCE'
import fcntl
import hashlib
import os
import sys
import time
from pathlib import Path

did, room = sys.argv[1], sys.argv[2]

state_dir = Path.home() / ".config" / "technocore" / "nonces"
state_dir.mkdir(parents=True, exist_ok=True)
os.chmod(state_dir, 0o700)

key = hashlib.sha256((did + "\0" + room).encode()).hexdigest()
state_file = state_dir / key

with state_file.open("a+", encoding="utf-8") as f:
    os.chmod(state_file, 0o600)
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    f.seek(0)
    raw = f.read().strip()
    last = int(raw) if raw else 0
    clock = time.time_ns() // 1_000_000
    nonce = max(last + 1, clock)

    f.seek(0)
    f.truncate()
    f.write(str(nonce) + "\n")
    f.flush()
    os.fsync(f.fileno())

    print(nonce)
PYNONCE
)"

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
