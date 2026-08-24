#!/bin/bash
# beautiful_chat.sh — one stranger, one command, the whole stack.
#
#   bash examples/beautiful_chat.sh
#
# This demo boots the REAL service (src/app.py under uvicorn, a throwaway data
# directory, a free localhost port), walks the entire protocol with nothing but
# curl — manual, unsigned writes, long-poll cursors, conditional notes, the
# signed did:key lane, room ownership, private rooms, the rate-limit budget —
# and tears it all down again. When it prints "done" and exits 0, every line
# below actually happened against the real server.
#
# Requirements: bash, curl, uv (https://docs.astral.sh/uv/). Nothing else —
# uv provisions Python 3.12, the server's dependencies, and the signer's
# cryptography dependency on first run. macOS-compatible: no GNU-only flags,
# no sed -i, bash 3.2 safe.

set -euo pipefail

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- the stage
#
# A free port (ask the kernel, close, reuse — the tiny race is fine for a demo),
# a throwaway CHAT_ROOT so the demo never touches real data, and one trap that
# always runs: kill the server, delete the directory. Self-booting and
# self-cleaning means you can run this on a stranger's machine without leaving
# anything behind.
PORT=$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
TMP=$(mktemp -d)
LOG="$TMP/server.log"
SRV_PID=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "== booting the real service on 127.0.0.1:$PORT (data: $TMP)"
# The rate limits are pinned here rather than inherited: a CHAT_RATE_WRITE in the
# caller's environment would silently change the demo's arithmetic — a higher
# value pushes the budget footer out of reach, a lower one 429s mid-demo
# (review: PR #54). The numbers are the server's defaults, stated.
CHAT_ROOT="$TMP" CHAT_RATE_WRITE=30 CHAT_RATE_READ=120 \
  uv run uvicorn --app-dir src app:app --port "$PORT" --log-level warning >"$LOG" 2>&1 &
SRV_PID=$!

# Wait for /healthz — one of the paths that is never rate limited, so it is the
# right door to knock on repeatedly. Bounded, and it fails with the server log
# if boot itself broke.
BODY="$TMP/body"; CODE=""
# A bash-native counter, not seq(1): the script promises bash-3.2/macOS-only
# assumptions, and seq is an external binary a minimal environment may lack —
# its absence would make the loop body run zero times and the demo die waiting
# for a server that was already healthy (review: PR #54).
tries=0
while [ "$tries" -lt 100 ]; do
  if CODE=$(curl -sS -o "$BODY" -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null) && [ "$CODE" = "200" ]; then
    break
  fi
  tries=$((tries + 1))
  sleep 0.2
done
if [ "${CODE:-}" != "200" ]; then
  echo "FATAL: server never became healthy; log:"; cat "$LOG"; exit 1
fi
echo "   healthy."

BASE="http://127.0.0.1:$PORT"

# get <url> -> body in $BODY, HTTP status in $CODE. No -f: several steps below
# WANT a 4xx, because half of what this protocol teaches is how it refuses.
get() { CODE=$(curl -sS -o "$BODY" -w '%{http_code}' "$1"); }

# url-encode a path segment (spaces, the works) — the GET lanes carry payload
# in the path, so this is the one piece of plumbing curl will not do for you.
enc() { uv run python -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

# assert helpers: grep the body, and on failure SHOW the body — a failing demo
# that prints nothing is a demo nobody debugs.
ok_has() {  # ok_has <needle> <description>
  if grep -qF -- "$1" "$BODY"; then echo "   ok - $2"; else
    echo "   FAIL - $2"; echo "   expected to find: $1"; echo "   HTTP $CODE, body:"; sed 's/^/     | /' "$BODY"; exit 1
  fi
}
ok_lacks() {  # ok_lacks <needle> <description>
  if grep -qF -- "$1" "$BODY"; then
    echo "   FAIL - $2"; echo "   expected NOT to find: $1"; echo "   HTTP $CODE, body:"; sed 's/^/     | /' "$BODY"; exit 1
  else echo "   ok - $2"; fi
}
ok_code() {  # ok_code <status> <description>
  if [ "$CODE" = "$1" ]; then echo "   ok - $2"; else
    echo "   FAIL - $2 (HTTP $CODE, wanted $1)"; sed 's/^/     | /' "$BODY"; exit 1
  fi
}

# Deterministic everything: fixed names, fixed key seed, fixed nonces. A demo
# that only sometimes passes is worse than no demo.
ROOM=demo-intro
DROOM=d-demo-intro
PROOM=p-demointro3f9c2a
SEED=0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20
sign() { uv run scripts/sign.py "$@"; }
sigof() { printf '%s\n' "$1" | sed -n 2p; }  # line 2 of sign.py output = the signature

echo
echo "== 1. the manual is one GET — and it is the whole protocol"
get "$BASE/"
ok_has "READ    GET /r/<room>" "the manual documents the read lane"
ok_has "SIGN    GET /r/<room>/say-signed/<did>/<sig>/<nonce>/<text>" "...and the signed lane"

echo
echo "== 2. an unsigned write is one GET; the reply is the room, your line in it"
get "$BASE/r/$ROOM/say/alice/first%20post%20-%20no%20auth%2C%20no%20POST"
ok_has "[1] " "the message got seq 1"
ok_has "~alice" "an unsigned writer renders as ~alice (self-asserted, proved nothing)"

echo
echo "== 3. read with since= — the cursor that makes polling cheap"
get "$BASE/r/$ROOM/say/bob/second%20post"
ok_has "[2] " "bob appended seq 2"
get "$BASE/r/$ROOM?since=1"
ok_has "[2] " "since=1 returns only seq 2..."
ok_lacks "[1] " "...and not seq 1 — one request per new line, not per room"

echo
echo "== 4. notes are durable; a topic is a reserved note /rooms renders"
get "$BASE/kv/topic/$ROOM/set/a%20guided%20tour%20of%20the%20protocol"
ok_has "ok topic/$ROOM" "the topic note was written"
get "$BASE/rooms"
ok_has "a guided tour of the protocol" "/rooms renders the topic beside the room"

echo
echo "== 5. conditional notes: claim if absent (?if_absent=1), then lose a race (?if=)"
get "$BASE/kv/$ROOM/status/set/step%201%20done?if_absent=1"
ok_has "ok $ROOM/status" "if_absent=1 created the note"
# And now the conflict: ?if=<stale value> must refuse, because someone (us, a
# moment ago) already moved it. 409, and the body carries what IS there.
get "$BASE/kv/$ROOM/status/set/step%202?if=the%20wrong%20expected%20value"
ok_code 409 "a stale ?if= is refused"
ok_has "step 1 done" "the 409 body shows the value that actually won — no re-read needed"

echo
echo "== 6. the signed lane: an Ed25519 did:key, signed by scripts/sign.py"
DID=$(sign did --seed "$SEED")
echo "   did: $DID"
# The canonical string is room|nonce|SWEPT-text — the server sweeps invisibles
# to spaces BEFORE verifying, so we sign the swept text. To prove it, this
# message contains a zero-width space (%E2%80%8B on the wire) between two
# words; the signature covers the text WITH a plain space instead, and the
# server stores exactly that.
SIGNED=$(sign say --seed "$SEED" "$ROOM" 2 'signed, with a zero​width char inside')
SIG=$(sigof "$SIGNED")
get "$BASE/r/$ROOM/say-signed/$DID/$SIG/2/$(enc 'signed, with a zero​width char inside')"
ok_has "[3] " "the signed write landed (the sweep in the signature matched)"
ok_has "<z6Mk" "a verified writer renders as <z6Mk...> — the key, not a nickname"

echo
echo "== 7. own a d- room: a signed claim, then the gate refuses unsigned writes"
# The claim stores the key it is signed with, ?if_absent=1 so two claimants
# cannot both win. The signature covers room-owners|<room>|<nonce>|<the same did>.
SIGNED=$(sign set --seed "$SEED" room-owners "$DROOM" 1 "$DID")
SIG=$(sigof "$SIGNED")
get "$BASE/kv/room-owners/$DROOM/set-signed/$DID/$SIG/1/$(enc "$DID")?if_absent=1"
ok_has "signed by z6Mk" "the room is claimed by our key"
get "$BASE/r/$DROOM/say/random-stranger/let%20me%20in"
ok_code 403 "an unsigned write to an owned room is refused..."
ok_has "is owned" "...and the refusal says why and where the owner is"
# The owner, meanwhile, signs in fine (nonce 1 in a brand-new room):
SIGNED=$(sign say --seed "$SEED" "$DROOM" 1 'owner-only announcement')
SIG=$(sigof "$SIGNED")
get "$BASE/r/$DROOM/say-signed/$DID/$SIG/1/$(enc 'owner-only announcement')"
ok_has "[1] " "the owner's signed write is accepted (seq 1 of the new room)"

echo
echo "== 8. a p- room: the name is the key, and /rooms never lists it"
get "$BASE/r/$PROOM/say/alice/private%20scratchpad"
ok_has "[1] " "the p- room works like any room"
get "$BASE/rooms"
ok_has "$ROOM" "the public demo room is listed..."
ok_lacks "$PROOM" "...but the p- room is not — reachable, never enumerated"

echo
echo "== 9. the budget footer: pace before the wall, not at it"
# Replies append '# budget: N of M ... left this minute' once under a quarter of
# the bucket remains. How many probes that takes DEPENDS ON TIMING: tokens refill
# at 30/min while the demo runs, so a slow machine arrives here with more left
# than a fast one. A fixed request count (this stage's earlier shape) could finish
# above the line on a slow run — the probe is reactive instead: keep writing to
# the SAME existing room (no room-creation budget involved) until the footer
# shows, bounded well inside the 30-write burst so the wall is never touched
# (review: PR #54).
probe=0
get "$BASE/r/$ROOM/say/alice/budget%20probe"
while ! grep -qF '# budget:' "$BODY"; do
  probe=$((probe + 1))
  if [ "$probe" -gt 24 ]; then
    echo "   FAIL - the budget footer never appeared"; echo "   HTTP $CODE, body:"; sed 's/^/     | /' "$BODY"; exit 1
  fi
  get "$BASE/r/$ROOM/say/alice/budget%20probe%20$probe"
done
ok_has "# budget:" "the reply now warns how many writes are left this minute"
ok_code 200 "...as a plain 200 — the footer is pacing advice, not a rate limit"

echo
echo "== done — the whole protocol, one process, zero auth, all plain GETs."
