#!/bin/bash
# signed_chat.sh — signed write lane, end to end.
#
#   bash examples/signed_chat.sh
#
# This extends beautiful_chat.sh's unsigned lane with the signed one: generate a
# key, read the nonce state for that key in the room, sign a message, post it,
# and then verify the stored record offline against the key — without asking the
# service to vouch for it. The signed lane proves authorship; the offline check
# proves the record the room serves is exactly what was signed.
#
# Requirements: bash, curl, uv (https://docs.astral.sh/uv/), openssl (for
# key generation).  Nothing else.

set -euo pipefail

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------- the stage
PORT=$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
TMP=$(mktemp -d)
LOG="$TMP/server.log"
SRV_PID=""
cleanup() {
    [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

echo "== booting the real service on 127.0.0.1:$PORT"
CHAT_ROOT="$TMP" \
  uv run uvicorn --app-dir src app:app --port "$PORT" --log-level warning >"$LOG" 2>&1 &
SRV_PID=$!

# Wait for /healthz — never rate limited, so it is the right door to knock on repeatedly.
# `if CODE=$(...)` rather than a bare assignment: under `set -e` a failing curl (exit 7
# while the port is still closed) would take the script with it on the first try, before
# the server had any chance to boot. A bash-native counter, not seq(1), for the same reason
# beautiful_chat.sh uses one: a minimal environment may lack it, and its absence would run
# the loop zero times and then blame a server that was healthy all along (review: PR #54).
CODE=""
tries=0
while [ "$tries" -lt 100 ]; do
    if CODE=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null) && [ "$CODE" = "200" ]; then
        break
    fi
    tries=$((tries + 1))
    sleep 0.2
done
if [ "${CODE:-}" != "200" ]; then
    echo "FATAL: server never became healthy; log:"
    cat "$LOG"
    exit 1
fi

BASE="http://127.0.0.1:$PORT"
ROOM="signed-demo-$$"

# Helpers
fail() { echo "FAIL: $1"; exit 1; }
ok_has() { grep -q "$1" <<<"$2" || fail "expected '$1' in: $2"; }

# The nonce this key must use next. Since the demo creates a unique fresh room with a
# fresh key, we track a local counter starting at 1 and increment after each use — no
# need to query the room.  Querying the room via `GET /r/$ROOM?format=json` would be
# wrong anyway: the API returns only the newest 50 messages, while the server's nonce
# rejection scans the newest 1 MiB (READ_BUDGET), so if this DID's last signed write is
# followed by >50 messages from other writers but is still inside 1 MiB, the query would
# see no nonce and return 1 while the server sees the prior nonce and rejects.
NONCE=1

# ---------------------------------------------------------------- key setup
echo "== generating Ed25519 key"
# 32 random bytes as 64 hex characters, which is what sign.py --seed takes directly as an
# Ed25519 seed. Throwaway: it exists for the length of this script.
DEMO_SEED=$(openssl rand -hex 32)
echo "   seed (first 8 chars): ${DEMO_SEED:0:8}..."

# Derive did:key from the seed using sign.py
DID=$(uv run python scripts/sign.py --seed "$DEMO_SEED" did)
echo "   did: $DID"

# ---------------------------------------------------------------- first signed write
echo "== first signed write"
echo "   nonce state for this key in $ROOM: using nonce=$NONCE"

TEXT="hello from the signed lane"
# sign the canonical string: room|nonce|swept-text
# sweep: this text has no invisible chars so it's unchanged
# sign.py say prints two lines — the did:key, then the signature. We already
# have the did from `did` above, so keep only the last line (the signature).
SIG=$(uv run python scripts/sign.py --seed "$DEMO_SEED" say "$ROOM" "$NONCE" "$TEXT" | tail -n1)
echo "   sig (first 20 chars): ${SIG:0:20}..."

RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/r/$ROOM/say-signed/$DID/$SIG/$NONCE/$(python3 -c "import urllib.parse; print(urllib.parse.quote('$TEXT'))")?format=json")
HTTP_STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
BODY=$(echo "$RESP" | grep -v "HTTP_STATUS")
echo "   HTTP $HTTP_STATUS  posted: $(python3 -c 'import json,sys; p=json.load(sys.stdin)["posted"]; print("seq",p["seq"],"nonce",p["nonce"])' <<<"$BODY")"

ok_has "HTTP_STATUS:200" "$RESP"
# `?format=json` on the write, not the default text view: the text lane abbreviates a DID to
# `z6Mk…XnDv` (didkey.abbreviate — a full one is ~1200 tokens on a 50-message fetch), so
# grepping the rendered page for the DID we just signed with would never match. The JSON lane
# carries it in full, which is also the lane a caller checking its own write wants.
python3 -c '
import json, sys
posted = json.load(sys.stdin)["posted"]
assert posted["from"] == sys.argv[1], posted
assert posted["sig"], "the record kept no signature to re-verify"
' "$DID" <<<"$BODY" || fail "the posted record is not attributed to $DID: $BODY"

# ---------------------------------------------------------------- second signed write — nonce must increase
echo "== second signed write (nonce increases locally)"
NONCE=$((NONCE + 1))
echo "   nonce state for this key in $ROOM: using nonce=$NONCE"
TEXT="second message, same key"
SIG2=$(uv run python scripts/sign.py --seed "$DEMO_SEED" say "$ROOM" "$NONCE" "$TEXT" | tail -n1)
RESP2=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/r/$ROOM/say-signed/$DID/$SIG2/$NONCE/$(python3 -c "import urllib.parse; print(urllib.parse.quote('$TEXT'))")")
ok_has "HTTP_STATUS:200" "$RESP2"

# ---------------------------------------------------------------- verify the records offline
echo "== verifying the stored records offline, against the key alone"
curl -sS "$BASE/r/$ROOM?format=json" >"$TMP/room.json"
echo "   room has $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["messages"]))' "$TMP/room.json") messages"

# The server verified `room|nonce|swept-text` at write time and stored the signature beside
# the record (src/store.py append). So the room JSON is self-contained: rebuild the same
# canonical string from the record, and check it under the DID's own public key. Nothing
# below talks to the service — that is the whole claim of the signed lane, that a record
# stays checkable by anyone holding the JSON. `cryptography` is a project dependency (it is
# what scripts/sign.py signs with), so `uv run` already has it.
uv run python - "$ROOM" "$DID" "$TMP/room.json" <<'PY'
import base64
import json
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTICODEC_ED25519 = b"\xed\x01"


def public_key(did):
    """The 32 raw Ed25519 bytes inside a did:key — base58btc, after the multicodec tag."""
    n = 0
    for ch in did.removeprefix("did:key:")[1:]:  # drop the 'z' multibase tag
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes(34, "big")
    if not raw.startswith(MULTICODEC_ED25519):
        sys.exit("not an ed25519 did:key")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


room, did, path = sys.argv[1], sys.argv[2], sys.argv[3]
key = public_key(did)
records = [
    m for m in json.load(open(path, encoding="utf-8"))["messages"] if m.get("from") == did
]
if not records:
    sys.exit("the room JSON carries no message from this DID")

for rec in records:
    if not rec.get("sig"):
        sys.exit("record %s has no stored signature to check" % rec["seq"])
    # Exactly what the server verified: the room, the nonce, and the text as stored.
    canonical = "%s|%s|%s" % (room, rec["nonce"], rec["text"])
    signature = base64.urlsafe_b64decode(rec["sig"] + "==")
    try:
        key.verify(signature, canonical.encode("utf-8"))
    except InvalidSignature:
        sys.exit("seq %s does NOT verify under %s" % (rec["seq"], did))
    print("   seq %s nonce %s verifies: %r" % (rec["seq"], rec["nonce"], rec["text"]))

# The control: the same signature over text one character different must fail, or the
# check above proves nothing.
tampered = "%s|%s|%s" % (room, records[0]["nonce"], records[0]["text"] + "!")
try:
    key.verify(base64.urlsafe_b64decode(records[0]["sig"] + "=="), tampered.encode("utf-8"))
except InvalidSignature:
    print("   tampered text is refused, as it must be")
else:
    sys.exit("a tampered message verified — the check above is meaningless")
PY

# ---------------------------------------------------------------- nonce reuse must fail
echo "== nonce reuse must be refused"
REUSE_TEXT="reused nonce, must be refused"
SIG_REUSE=$(uv run python scripts/sign.py --seed "$DEMO_SEED" say "$ROOM" "$NONCE" "$REUSE_TEXT" | tail -n1)
RESP_REUSE=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" \
    "$BASE/r/$ROOM/say-signed/$DID/$SIG_REUSE/$NONCE/$(python3 -c "import urllib.parse; print(urllib.parse.quote('$REUSE_TEXT'))")")
HTTP_REUSE=$(echo "$RESP_REUSE" | grep "HTTP_STATUS" | cut -d: -f2)
REUSE_BODY=$(echo "$RESP_REUSE" | grep -v "HTTP_STATUS")
echo "   HTTP $HTTP_REUSE  body: $REUSE_BODY"
# The signature is valid and the text matches — the only thing the server can refuse here
# is the replayed nonce. The nonce-replay error names the nonce in the body (400 "nonce N
# is not greater than M..."), while a signature error would say "bad signature".
ok_has "HTTP_STATUS:400" "$RESP_REUSE"
ok_has "nonce" "$REUSE_BODY"

# ---------------------------------------------------------------- show the rendering
echo "== rendered room view"
curl -sS "$BASE/r/$ROOM" | head -5

echo ""
echo "done — signed lane works, and its records verify offline"
