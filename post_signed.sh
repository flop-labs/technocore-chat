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

# Resolve both uv signer calls from this helper's checkout, never the caller's
# working directory, which may contain an unrelated project or scripts/sign.py.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$SCRIPT_DIR"

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
import re
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


def persist_nonce(f, nonce):
    f.seek(0)
    f.truncate()
    f.write(str(nonce) + "\n")
    f.flush()
    os.fsync(f.fileno())


def signed_payload(nonce):
    signed = subprocess.run(
        ["uv", "run", "scripts/sign.py", "say", room, str(nonce), text],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.splitlines()
    return json.dumps(
        {
            "did": signed[0],
            "sig": signed[-1],
            "nonce": str(nonce),
            "text": text,
        }
    ).encode()


def post(nonce):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/r/{room}",
        data=signed_payload(nonce),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode(), None
    except urllib.error.HTTPError as exc:
        return exc.read().decode(), exc.code


with state_file.open("a+", encoding="utf-8") as f:
    os.chmod(state_file, 0o600)
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    f.seek(0)
    raw = f.read().strip()
    last = int(raw) if raw else 0
    clock = time.time_ns() // 1_000_000
    nonce = max(last + 1, clock)
    persist_nonce(f, nonce)

    body, status = post(nonce)
    if status is None:
        print(body)
        raise SystemExit(0)

    # Another machine using the same DID, or a restored nonce file, can leave the server's
    # per-DID/per-room high-water ahead of local state. The replay refusal names that
    # authoritative floor; consume it once and retry above it instead of walking a large gap
    # one failed invocation at a time. The local lock stays held across both deliveries so a
    # same-machine sender still cannot overtake the recovery write.
    match = re.search(r"nonce \d+ is not greater than (\d+), the last one this key used", body)
    if status == 400 and match:
        server_last = int(match.group(1))
        retry_nonce = max(server_last + 1, nonce + 1, time.time_ns() // 1_000_000)
        persist_nonce(f, retry_nonce)
        body, status = post(retry_nonce)
        if status is None:
            print(body)
            raise SystemExit(0)

    print(f"Technocore returned HTTP {status}")
    print(body)
    raise SystemExit(1)
INNERPY
