# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""Offline verifier for technocore-chat's signed lane — the other half of scripts/sign.py.

Standalone like sign.py: 'uv run scripts/verify.py ...' provisions its own
cryptography dependency from the PEP 723 header above, so anyone holding a
stored record can re-verify it with no checkout, no venv and no server.

Why this exists: a signed record stores the DID but not the signature (src/
didkey.py §5.4). The server verifies once at write time and is trusted
afterwards; every later re-verification needs the canonical string rebuilt by
the reader. The manual documents that string ("SIGNING"), but rebuilding it by
hand is exactly where mistakes hide — sweep order, separator placement, the
URL-decoded text. This script does it once, correctly, offline.

Canonical strings (identical to what _signer checks server-side):

    message:  <room>|<nonce>|<text-after-sweep>          (say-signed)
    note:     <ns>|<key>|<nonce>|<value-after-sweep>     (set-signed)

Usage:
  uv run scripts/verify.py say  <did> <sig> <nonce> <room> <text>
  uv run scripts/verify.py set  <did> <sig> <nonce> <ns> <key> <value>
  uv run scripts/verify.py did  <did>

'say'/'set' print "OK <did>" on success; any failure exits non-zero with the
reason. 'did' just parses a DID and prints the raw public key in hex — useful
when key-matching records across rooms.

Exit codes: 0 verified · 1 nonce format invalid · 2 malformed DID/signature/missing arguments · 3 signature does not cover the message.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import unicodedata

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PREFIX = "did:key:z6Mk"
MULTICODEC_ED25519 = b"\xed\x01"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTIBASE_CHARS = 48
SIG_CHARS = 86  # 64 raw bytes, base64url, unpadded

INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
MAX_TEXT_CHARS = 4096  # messages
MAX_VALUE_CHARS = 8192  # notes

def _die2(msg: str) -> None:
    """Exit 2 with reason on stderr — python's SystemExit(2, msg) prints to stdout."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)

def swept(text: str, limit: int) -> str:
    """The single-line sweep from src/store.py clean_text: invisibles -> spaces,
    ends trimmed. A stored text is already swept, so this normalizes whatever the
    caller pasted (e.g. a line-wrap introduced by copy-paste) to stored bytes."""
    cleaned = "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()
    if not cleaned or len(cleaned) > limit:
        raise SystemExit(f"text must be 1..{limit} visible characters after the sweep")
    return cleaned


def public_key(did: str) -> bytes:
    """The 32 raw Ed25519 public-key bytes of a `did:key`, or exit 2."""
    if not isinstance(did, str) or not did.startswith(PREFIX):
        _die2("bad did:key: must start with did:key:z6Mk")
    mb = did[len("did:key:") :]
    if len(mb) != MULTIBASE_CHARS or not mb.startswith("z"):
        _die2(f"bad did:key: expected {MULTIBASE_CHARS} multibase chars starting z, got {len(mb)}")
    n = 0
    for ch in mb[1:]:
        digit = B58.find(ch)
        if digit < 0:
            _die2(f"bad did:key: bad base58 char {ch!r}")
        n = n * 58 + digit
    decoded = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    if len(decoded) != 34 or not decoded.startswith(MULTICODEC_ED25519):
        _die2("bad did:key: only ed25519-pub (z6Mk...) keys are accepted")
    return decoded[2:]


def check_sig_encoding(sig: str) -> bytes:
    """Decode an 86-character unpadded base64url signature, or exit 2."""
    if not re.fullmatch(rf"[A-Za-z0-9_-]{{{SIG_CHARS}}}", sig or ""):
        _die2(f"bad signature: expected {SIG_CHARS} base64url characters")
    return base64.urlsafe_b64decode(sig[:SIG_CHARS] + "==")


def verify(did: str, sig: str, canonical: str) -> None:
    """Exit 3 unless `sig` verifies over `canonical` for `did`. Mirrors src/didkey.py
    verdicts: malformed input exits 2, a well-formed pair that does not match exits 3."""
    key = Ed25519PublicKey.from_public_bytes(public_key(did))
    try:
        key.verify(check_sig_encoding(sig), canonical.encode("utf-8"))
    except InvalidSignature:
        raise SystemExit(3) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    say = sub.add_parser("say", help="verify room|nonce|swept-text")
    say.add_argument("did")
    say.add_argument("sig")
    say.add_argument("nonce")
    say.add_argument("room")
    say.add_argument("text")

    note = sub.add_parser("set", help="verify ns|key|nonce|swept-value")
    note.add_argument("did")
    note.add_argument("sig")
    note.add_argument("nonce")
    note.add_argument("ns")
    note.add_argument("key")
    note.add_argument("value")

    only_did = sub.add_parser("did", help="parse a DID, print the raw public key in hex")
    only_did.add_argument("did")

    args = parser.parse_args()

    if args.cmd == "did":
        print(public_key(args.did).hex())
        return

    # ASCII digits only, exactly the server's NONCE_RE — same reason as sign.py.
    if not re.fullmatch(r"[0-9]{1,19}", args.nonce):
        parser.exit(1, f"nonce must be 1-19 ASCII digits, got {args.nonce!r}\n")

    if args.cmd == "say":
        canonical = f"{args.room}|{args.nonce}|{swept(args.text, MAX_TEXT_CHARS)}"
    else:
        canonical = f"{args.ns}|{args.key}|{args.nonce}|{swept(args.value, MAX_VALUE_CHARS)}"

    verify(args.did, args.sig, canonical)
    print(f"OK {args.did}")


if __name__ == "__main__":
    main()
