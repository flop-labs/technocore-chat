#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/flop-labs/technocore-chat.git"
REPO_DIR="$HOME/technocore-chat"
SEED_DIR="$HOME/.config/technocore"
SEED_FILE="$SEED_DIR/sign_seed"

echo "Technocore safe onboarding helper"
echo

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git is not installed."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Error: uv is not installed."
  echo "Install it with:"
  echo "curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "Cloning Technocore..."
  git clone "$REPO_URL" "$REPO_DIR"
else
  echo "Technocore repo already exists."
fi

cd "$REPO_DIR"

echo "Installing locked dependencies..."
uv sync

mkdir -p "$SEED_DIR"
chmod 700 "$SEED_DIR"

if [[ ! -f "$SEED_FILE" ]]; then
  echo "Creating a new private Ed25519 seed..."
  umask 077
  python3 - <<'PY'
from pathlib import Path
import secrets

seed_file = Path.home() / ".config" / "technocore" / "sign_seed"
seed_file.write_text(secrets.token_hex(32) + "\n")
PY
  chmod 600 "$SEED_FILE"
  echo "New seed created."
else
  echo "Existing seed preserved."
fi

PERMS="$(stat -c '%a' "$SEED_FILE")"

if [[ "$PERMS" != "600" ]]; then
  echo "Fixing seed permissions..."
  chmod 600 "$SEED_FILE"
fi

echo
echo "Public DID:"
SIGN_SEED="$(cat "$SEED_FILE")" uv run scripts/sign.py did

echo
echo "Setup complete."
echo "Private seed location:"
echo "$SEED_FILE"
echo
echo "Never share, upload, or screenshot the seed."
echo
echo "To run Technocore locally:"
echo "cd $REPO_DIR"
echo "CHAT_ROOT=./data uv run uvicorn --app-dir src app:app --port 8080"
