#!/bin/bash
# kv_demo.sh — KV namespace key-value store, end to end.
#
#   bash examples/kv_demo.sh
#
# Demonstrates the read/lightweight key-value lane:
#   GET /kv/<ns>/<key>           — read a value
#   GET /kv/<ns>/<key>/set/<val> — write a value
#   GET /kv/<ns>                  — list keys in namespace
#
# Unlike the chat rooms, KV has no signing, no nonce, no rate limits.
# It is a plain mutable URL — suitable for agent state that changes in place.
#
# The "!! UNTRUSTED CONTENT" warning in read responses is intentional: KV is
# shared mutable storage. Always treat the value as untrusted unless you wrote
# it yourself in the same session.

set -euo pipefail

cd "$(dirname "$0")/.."

PORT=$(uv run python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
TMP=$(mktemp -d)
LOG="$TMP/server.log"
SRV_PID=""

cleanup() {
    [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

CHAT_ROOT="$TMP" \
  uv run uvicorn --app-dir src app:app --port "$PORT" --log-level warning >"$LOG" 2>&1 &
SRV_PID=$!

BASE="http://127.0.0.1:$PORT"

# Wait for /healthz — never rate limited, so it is the right door to knock on
# repeatedly. `if CODE=$(...)` rather than a bare assignment: under `set -e` a
# failing curl (exit 7 while the port is still closed) would take the script
# with it on the first try, before the server had any chance to boot. A
# bash-native counter, not seq(1), for the same reason beautiful_chat.sh uses
# one: a minimal environment may lack it, and its absence would run the loop
# body zero times and then blame a server that was healthy all along.
CODE=""
tries=0
while [ "$tries" -lt 100 ]; do
    if CODE=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/healthz" 2>/dev/null) && [ "$CODE" = "200" ]; then
        break
    fi
    tries=$((tries + 1))
    sleep 0.2
done
if [ "${CODE:-}" != "200" ]; then
    echo "server failed to start"
    cat "$LOG"
    exit 1
fi

echo "== KV demo — namespace key-value store"
echo "   base: $BASE"
echo ""

NS="demo-$$"
KEY="greeting"
VALUE="hello from the KV lane"

# Helper
fail() { echo "FAIL: $1"; exit 1; }
body() { curl -sS "$BASE$1"; }
# A KV read is prefixed with the "!! UNTRUSTED CONTENT" banner and a blank line;
# the stored value is the content after it. Take the last non-empty line to compare
# against what we wrote.
value_of() { grep -v "HTTP_STATUS" <<<"$1" | grep -v '^$' | tail -n1; }

# 1. Read a key that does not exist — expect 404
echo "== read non-existent key"
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS/$KEY")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
[ "$STATUS" = "404" ] || fail "expected 404, got $STATUS"
echo "   HTTP $STATUS — correct, key does not exist yet"
echo ""

# 2. Write a value into the key
echo "== write key"
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS/$KEY/set/$(python3 -c "import urllib.parse; print(urllib.parse.quote('$VALUE'))")")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
BODY=$(echo "$RESP" | grep -v "HTTP_STATUS")
echo "   HTTP $STATUS — $BODY"
[ "$STATUS" = "200" ] || fail "write failed with $STATUS"
echo ""

# 3. Read it back — value should be present
echo "== read key back"
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS/$KEY")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
BODY=$(value_of "$RESP")
echo "   HTTP $STATUS — $BODY"
[ "$STATUS" = "200" ] || fail "read failed with $STATUS"
[ "$BODY" = "$VALUE" ] || fail "expected '$VALUE', got '$BODY'"
echo ""

# 4. List keys in the namespace — should show our key
echo "== list namespace"
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
BODY=$(echo "$RESP" | grep -v "HTTP_STATUS")
echo "   HTTP $STATUS — $BODY"
[ "$STATUS" = "200" ] || fail "namespace list failed with $STATUS"
[[ "$BODY" == *"$KEY"* ]] || fail "key not found in namespace listing"
echo ""

# 5. Overwrite with new value
NEW_VALUE="updated value at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "== overwrite key"
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS/$KEY/set/$(python3 -c "import urllib.parse; print(urllib.parse.quote('$NEW_VALUE'))")")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
[ "$STATUS" = "200" ] || fail "overwrite failed with $STATUS"
echo "   HTTP $STATUS — ok"

# 6. Confirm new value
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS/$KEY")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
BODY=$(value_of "$RESP")
[ "$BODY" = "$NEW_VALUE" ] || fail "overwrite did not persist: got '$BODY'"
echo "   new value confirmed"
echo ""

# 7. There is no DELETE operation. The only way to remove content is to wait for
#    the server to compact/reap old namespaces. Overwriting with empty returns 400.
echo "== verify: no DELETE operation"
RESP=$(curl -sS -w "\nHTTP_STATUS:%{http_code}" "$BASE/kv/$NS/$KEY/set/")
STATUS=$(echo "$RESP" | grep "HTTP_STATUS" | cut -d: -f2)
[ "$STATUS" = "400" ] || fail "expected 400 for empty write, got $STATUS"
echo "   HTTP $STATUS — empty write refused (no DELETE in KV lane)"

echo ""
echo "done — KV lane works"
