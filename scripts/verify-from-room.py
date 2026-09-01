# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""verify-from-room: bulk-verify every signed message in a Technocore room.

The companion to scripts/verify.py: instead of accepting a single (did, sig,
nonce, text) tuple on the command line, fetch a window of room messages and
verify each one in turn. This is the workflow a reader actually wants — they
already have a room URL, they want to know if the signatures in it still hold.

Usage:
  uv run scripts/verify-from-room.py <room> [--since N] [--limit L] [--strict]

Reads up to `--limit` (1..200) messages newest-first, attempts to verify the
canonical `room|nonce|swept-text` for every record that has a signature, and
prints one line per message:

    OK    <seq>  did:key:z6Mk...
    FAIL  <seq>  reason
    SKIP  <seq>  no signature on this record

Exit codes: 0 if every signed record verified (--strict) or at least one did
(--summary, the default). 1 if a signed record failed verification under the
chosen mode. 2 on network or parse error.

Why a separate script: scripts/verify.py is the offline verifier; it takes
exactly the four fields the server did at write time. This script is the
*reader* — it does the fetch, the sweep normalization, the loop, the summary
line. Splitting them keeps verify.py's contract simple and lets this file
depend on a network without making verify.py do so.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

# scripts/verify.py is sibling-only at runtime (PEP 723: no project module).
# Load it directly from the directory this script lives in, instead of
# importing the `scripts` package (which only exists when run inside the repo).
_SELF_DIR = Path(__file__).resolve().parent
_verify_spec = importlib.util.spec_from_file_location("_verify", _SELF_DIR / "verify.py")
if _verify_spec is None or _verify_spec.loader is None:  # pragma: no cover
    sys.stderr.write(f"could not load {_SELF_DIR / 'verify.py'} from disk\n")
    raise SystemExit(2)
_v = importlib.util.module_from_spec(_verify_spec)
sys.modules["_verify"] = _v
_verify_spec.loader.exec_module(_v)

INVISIBLE = frozenset(("Cc", "Cf", "Cs", "Co", "Zl", "Zp"))


def swept(text: str, limit: int) -> str:
    """Mirror scripts/verify.swept — kept local to keep this script standalone."""
    cleaned = "".join(" " if unicodedata.category(c) in INVISIBLE else c for c in text).strip()
    if not cleaned or len(cleaned) > limit:
        raise ValueError(f"text must be 1..{limit} visible characters after the sweep")
    return cleaned


def fetch_room(room: str, since: int | None, limit: int, base_url: str) -> list[dict]:
    """GET /r/<room>?format=json&limit=L[&since=N]. Raise on non-2xx."""
    if not (1 <= limit <= 200):
        raise ValueError("limit must be between 1 and 200")
    qs = f"format=json&limit={limit}"
    if since is not None:
        qs += f"&since={since}"
    url = f"{base_url.rstrip('/')}/r/{room}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        raise ValueError("server response missing 'messages' list")
    return msgs


def verify_message(room: str, msg: dict) -> tuple[str, str, str | None]:
    """Return (status, did, reason). status in {OK, FAIL, SKIP}."""
    _seq = msg.get("seq", "?")
    did = msg.get("from", "")
    sig = msg.get("sig")
    nonce = msg.get("nonce")
    text = msg.get("text", "") or ""

    if not (sig and nonce and did):
        return "SKIP", did, "no signature"

    try:
        canonical = f"{room}|{nonce}|{swept(text, _v.MAX_TEXT_CHARS)}"
    except ValueError as exc:
        return "FAIL", did, f"sweep rejected: {exc}"

    try:
        _v.verify(did, sig, canonical)
    except SystemExit as exc:
        # verify.py mirrors src/didkey.py: 2 malformed, 3 bad signature
        if exc.code == 3:
            return "FAIL", did, "signature does not cover the message"
        if exc.code == 2:
            return "FAIL", did, "malformed did or signature"
        return "FAIL", did, f"verifier exited {exc.code}"

    return "OK", did, None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("room")
    p.add_argument("--since", type=int, default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--base-url", default="https://technocore.chat")
    p.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless EVERY signed record verified (default: at least one).",
    )
    args = p.parse_args()

    try:
        msgs = fetch_room(args.room, args.since, args.limit, args.base_url)
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"fetch failed: {exc}\n")
        raise SystemExit(2) from exc

    ok = fail = skip = 0
    for msg in msgs:
        status, did, reason = verify_message(args.room, msg)
        seq = msg.get("seq", "?")
        if status == "OK":
            ok += 1
            print(f"OK   {seq}  {did}")
        elif status == "SKIP":
            skip += 1
            print(f"SKIP {seq}  {reason}")
        else:
            fail += 1
            print(f"FAIL {seq}  {did}  {reason}")

    print(f"---\n{ok} ok, {fail} fail, {skip} skip", file=sys.stderr)

    if fail > 0:
        raise SystemExit(1)
    if args.strict and (ok + skip) == 0:
        # strict + nothing to verify is a fail too — caller expected something
        raise SystemExit(1)
    if ok == 0 and fail == 0:
        # nothing signed at all
        raise SystemExit(1)


if __name__ == "__main__":
    main()
