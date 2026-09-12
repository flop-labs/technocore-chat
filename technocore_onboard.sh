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
  ORIGIN_URL="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
  CANONICAL_ORIGIN="${ORIGIN_URL%/}"
  CANONICAL_ORIGIN="${CANONICAL_ORIGIN%.git}"

  case "$CANONICAL_ORIGIN" in
    "https://github.com/flop-labs/technocore-chat"|"git@github.com:flop-labs/technocore-chat"|"ssh://git@github.com/flop-labs/technocore-chat")
      ;;
    *)
      echo "Error: refusing to use existing checkout at $REPO_DIR." >&2
      echo "Its origin is not the official flop-labs/technocore-chat repository." >&2
      echo "Found origin: ${ORIGIN_URL:-<missing>}" >&2
      echo "Expected: $REPO_URL" >&2
      exit 1
      ;;
  esac

  echo "Technocore repo already exists with verified official origin."
fi

# A remote name alone does not authenticate the local code that receives the
# seed. Check the currently advertised official main commit without updating
# the user's refs, index or working tree (also safe for concurrent onboarding).
checked_git() {
  git --no-replace-objects -c core.fsmonitor=false -C "$REPO_DIR" "$@"
}

if [[ "$(checked_git ls-remote --get-url "$REPO_URL")" != "$REPO_URL" ]]; then
  echo "Error: refusing a rewritten official repository URL." >&2
  exit 1
fi

if ! UPSTREAM_RECORD="$(checked_git ls-remote --exit-code "$REPO_URL" refs/heads/main)"; then
  echo "Error: cannot verify official main; refusing dependency execution and seed use." >&2
  exit 1
fi
UPSTREAM_SHA="${UPSTREAM_RECORD%%$'\t'*}"
if [[ ! "$UPSTREAM_SHA" =~ ^[0-9a-f]{40}$ ||
      "$UPSTREAM_RECORD" != "$UPSTREAM_SHA"$'\t'"refs/heads/main" ]]; then
  echo "Error: unexpected official main response; refusing seed use." >&2
  exit 1
fi

verify_checkout() {
  local head status path expected actual
  head="$(checked_git rev-parse --verify 'HEAD^{commit}')"
  if [[ "$head" != "$UPSTREAM_SHA" ]]; then
    echo "Error: checkout HEAD does not match verified upstream main." >&2
    echo "Use a clean checkout of official main; local work has not been changed." >&2
    exit 1
  fi
  status="$(checked_git status --porcelain=v1 --untracked-files=all)"
  if [[ -n "$status" ]]; then
    echo "Error: checkout has local changes or untracked files; refusing seed use." >&2
    echo "Preserve your work separately and use a clean checkout of official main." >&2
    exit 1
  fi
  # Check the raw execution inputs too: index flags or Git filters must not
  # make a modified signer/dependency definition appear safe to git status.
  for path in scripts/sign.py pyproject.toml uv.lock; do
    expected="$(checked_git rev-parse --verify "$UPSTREAM_SHA:$path")"
    if [[ ! -f "$REPO_DIR/$path" || -L "$REPO_DIR/$path" ]]; then
      echo "Error: $path is not a regular upstream file; refusing seed use." >&2
      exit 1
    fi
    actual="$(checked_git hash-object --no-filters -- "$REPO_DIR/$path")"
    if [[ "$actual" != "$expected" ]]; then
      echo "Error: $path differs from verified upstream content; refusing seed use." >&2
      exit 1
    fi
  done
}

verify_checkout
cd "$REPO_DIR"

echo "Installing locked dependencies..."
uv sync --frozen

mkdir -p "$SEED_DIR"
chmod 700 "$SEED_DIR"

SEED_STATUS="$(python3 - <<'PY'
import os
import secrets
import tempfile
from pathlib import Path

seed_dir = Path.home() / ".config" / "technocore"
seed_file = seed_dir / "sign_seed"

# Prepare a complete private seed away from the public path, then publish it
# with a same-filesystem hard link. link(2) is atomic and fails if another
# onboarding process already won, so the public path is never visible empty
# or partially written and every loser converges on the winner's seed.
fd, tmp_name = tempfile.mkstemp(prefix=".sign_seed.", dir=seed_dir)
tmp_path = Path(tmp_name)
created = False

try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as handle:
        fd = -1
        handle.write((secrets.token_hex(32) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())

    try:
        os.link(tmp_path, seed_file)
        created = True
    except FileExistsError:
        # Another process atomically published first. Its complete seed is the
        # canonical identity; discard this candidate without ever exposing it.
        pass

    # Whether this process published the seed or observed another process's
    # winning link, make the parent directory entry durable before reporting
    # success. This closes the loser-side window where a DID could be reported
    # before any process had fsynced the directory entry.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(seed_dir, flags)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
finally:
    if fd != -1:
        os.close(fd)
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass

print("created" if created else "existing")
PY
)"

if [[ "$SEED_STATUS" == "created" ]]; then
  echo "New seed created."
else
  echo "Existing seed preserved."
fi

PERMS="$(stat -c '%a' "$SEED_FILE")"

if [[ "$PERMS" != "600" ]]; then
  echo "Error: seed permissions are $PERMS; expected 600." >&2
  echo "Refusing to use this seed because it may already have been exposed." >&2
  echo "Do not repair permissions and continue with this DID; rotate to a new seed/DID instead." >&2
  exit 1
fi

# Do not hand the seed to code changed during dependency setup either.
verify_checkout

echo
echo "Public DID:"
SIGN_SEED="$(cat "$SEED_FILE")" uv run --frozen scripts/sign.py did

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
