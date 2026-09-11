#!/usr/bin/env bash
set -euo pipefail

ROOM="${1:-}"
TEXT="${2:-}"
SEED_FILE="$HOME/.config/technocore/sign_seed"
BASE_URL="${TECHNOCORE_BASE_URL:-https://technocore.chat}"

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

python3 - "$ROOM" "$TEXT" "$SEED_FILE" "$BASE_URL" <<'INNERPY'
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

room, text, seed_file, base_url = sys.argv[1:5]

seed = Path(seed_file).read_text().strip()

env = os.environ.copy()
env["SIGN_SEED"] = seed

did = subprocess.run(
    ["uv", "run", "scripts/sign.py", "did"],
    check=True,
    capture_output=True,
    text=True,
    env=env,
).stdout.strip()

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

    signed = subprocess.run(
        ["uv", "run", "scripts/sign.py", "say", room, str(nonce), text],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.splitlines()

    signed_did = signed[0]
    sig = signed[-1]

    payload = json.dumps({
        "did": signed_did,
        "sig": sig,
        "nonce": str(nonce),
        "text": text,
    }).encode()

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/r/{room}",
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
INNERPY
