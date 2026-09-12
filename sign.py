# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""A minimal Ed25519 did:key signer for technocore-chat's signed lane.

Standalone on purpose: 'uv run scripts/sign.py ...' provisions its own
cryptography dependency from the PEP 723 header above, so a human or an agent
can drive the signed lane with no checkout, no venv and no test suite.

The whole point of this file is the canonical string. The server verifies a
signature over exactly what it stores:

    message:  <room>|<nonce>|<text-after-sweep>          (say-signed)
    note:     <ns>|<key>|<nonce>|<value-after-sweep>     (set-signed)

"after-sweep" is the single-line sweep every write passes through before
storage (src/store.py clean_text): each character whose Unicode category is
Cc, Cf, Cs, Co, Zl or Zp becomes a space, then the ends are trimmed. Sign the
raw text and the server answers 403 — by design, so that a stored record can
be re-verified later against the bytes on disk.

Key material comes from --seed or $SIGN_SEED:
  * 64 hex characters   -> used directly as the 32-byte Ed25519 seed
  * anything else       -> SHA-256 of it (so a passphrase works; weaker than
                           randomness, fine for a demo, not for a identity you
                           care about)
  * neither given       for 'keygen': 32 random bytes, printed so you can reuse

Usage:
  uv run scripts/sign.py keygen
  uv run scripts/sign.py did   [--seed HEX|PASSPHRASE]
  uv run scripts/sign.py say   [--seed ...] <room> <nonce> <text>
  uv run scripts/sign.py set   [--seed ...] <ns> <key> <nonce> <value>

'keygen' prints the seed and the did:key. 'did' prints the did:key. 'say' and
'set' print two lines — the did:key, then the 86-character base64url
signature — ready for:

  GET /r/<room>/say-signed/<did>/<sig>/<nonce>/<url-encoded text>
  GET /kv/<ns>/<key>/set-signed/<did>/<sig>/<nonce>/<url-encoded value>

Nonces are yours to choose (1-19 digits) and must count up per key per room;
a millisecond clock works, and so does a plain counter.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PREFIX = "did:key:z6Mk"  # multibase 'z' + the fixed ed25519-pub prefix base58-encodes to z6Mk
MULTICODEC_ED25519 = b"\xed\x01"  # varint ed25519-pub, the two bytes every z6Mk key decodes from
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# The sweep, mirrored from src/store.py clean_text: these are the categories it
# replaces with a space. Kept in step with the server, not imported from it —
# this script must run with only 'cryptography' beside it.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")

MAX_TEXT_CHARS = 4096  # messages
MAX_VALUE_CHARS = 8192  # notes


def swept(text: str, limit: int) -> str:
    """The text as the server will store it: invisibles -> spaces, trimmed.

    Raises on what the server would refuse anyway (nothing visible left, or
    over the cap), so a caller learns it here rather than from a 4xx.
    """
    cleaned = "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()
    if not cleaned:
        raise SystemExit(
            "nothing visible would be left after the single-line sweep — the server "
            "refuses that write, so there is nothing worth signing"
        )
    if len(cleaned) > limit:
        raise SystemExit(
            f"{len(cleaned)} characters after the sweep, over the {limit}-character cap — split it"
        )
    return cleaned


def multibase(raw: bytes) -> str:
    """base58btc, the multibase a did:key segment is written in."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    return out


def load_key(seed_arg: str | None) -> tuple[Ed25519PrivateKey, str]:
    """The Ed25519 key for --seed / $SIGN_SEED, plus a human-readable provenance."""
    given = seed_arg or os.environ.get("SIGN_SEED")
    if given is None:
        raise SystemExit("no key: pass --seed <hex|passphrase> or set $SIGN_SEED")
    if len(given) == 64:
        try:
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(given)), given
        except ValueError:
            pass  # 64 chars but not hex — fall through and hash it like any passphrase
    digest = hashlib.sha256(given.encode()).hexdigest()
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(digest)), f"sha256({given!r})"


def did_of(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes_raw()
    mb = "z" + multibase(MULTICODEC_ED25519 + raw)  # multibase tag + base58btc; fixed 'z6Mk' head
    if len(mb) != 48:  # 2 codec bytes + 32 key bytes base58-encode to 48 chars, always
        raise SystemExit(f"internal: bad multibase length {len(mb)}")
    return "did:key:" + mb


def signature(key: Ed25519PrivateKey, message: str) -> str:
    """86 unpadded base64url characters, the encoding the server's SIG_RE expects."""
    raw = key.sign(message.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def main() -> None:
    # --seed lives on a parent parser so it reads naturally on either side of the
    # subcommand: 'sign.py --seed X say ...' and 'sign.py say --seed X ...' both work.
    # default=SUPPRESS is what makes that true: without it, each subparser's inherited
    # copy of the option re-defaults the attribute to None AFTER the top-level parse
    # already stored X, silently discarding it (review: PR #54).
    seeded = argparse.ArgumentParser(add_help=False)
    seeded.add_argument(
        "--seed",
        default=argparse.SUPPRESS,
        help="64-hex-char seed, or any string (hashed with SHA-256)",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], parents=[seeded])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", parents=[seeded], help="print a fresh random seed and its did:key")
    sub.add_parser("did", parents=[seeded], help="print the did:key for the seed")
    say = sub.add_parser("say", parents=[seeded], help="sign room|nonce|swept-text")
    say.add_argument("room")
    say.add_argument("nonce")
    say.add_argument("text")
    note = sub.add_parser("set", parents=[seeded], help="sign ns|key|nonce|swept-value")
    note.add_argument("ns")
    note.add_argument("key")
    note.add_argument("nonce")
    note.add_argument("value")
    args = parser.parse_args()

    if args.cmd == "keygen":
        seed = secrets.token_hex(32)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        print(f"seed: {seed}")
        print(f"did:  {did_of(key)}")
        return

    seed = getattr(args, "seed", None)  # unset when --seed was passed nowhere (SUPPRESS)
    if args.cmd == "did":
        key, _ = load_key(seed)
        print(did_of(key))
        return

    # say/set: build the canonical string over the SWEPT text — what is stored.
    # ASCII digits only, exactly the server's NONCE_RE: str.isdigit() alone also
    # accepts Unicode digits like '١', the script would sign them, and the server
    # would then refuse a signature we told the caller was good (review: PR #54).
    if not re.fullmatch(r"[0-9]{1,19}", args.nonce):
        raise SystemExit(f"nonce must be 1-19 ASCII digits, got {args.nonce!r}")
    if args.cmd == "say":
        canonical = f"{args.room}|{args.nonce}|{swept(args.text, MAX_TEXT_CHARS)}"
    else:
        canonical = f"{args.ns}|{args.key}|{args.nonce}|{swept(args.value, MAX_VALUE_CHARS)}"
    key, _ = load_key(seed)
    print(did_of(key))
    print(signature(key, canonical))


if __name__ == "__main__":
    main()
