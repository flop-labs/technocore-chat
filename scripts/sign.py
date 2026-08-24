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

Key material comes from --seed-file, --seed or $SIGN_SEED:
  * --seed-file PATH   -> one seed/passphrase line read directly from a private
                          file, without putting it in argv or the environment
  * 64 hex characters   -> used directly as the 32-byte Ed25519 seed
  * anything else       -> SHA-256 of it (so a passphrase works; weaker than
                           randomness, fine for a demo, not for a identity you
                           care about)
  * neither given       for 'keygen': 32 random bytes, printed so you can reuse

On keygen, --seed-file PATH creates PATH exclusively with a fresh random seed
and prints only the path and DID. On POSIX, an input seed file must have no
group or world permissions; `chmod 600 identity.seed` is the normal setting.

Usage:
  uv run scripts/sign.py keygen [--seed-file PATH]
  uv run scripts/sign.py did   [--seed-file PATH|--seed HEX|PASSPHRASE]
  uv run scripts/sign.py say   [--seed-file PATH|--seed ...] <room> <nonce> <text>
  uv run scripts/sign.py set   [--seed-file PATH|--seed ...] <ns> <key> <nonce> <value>

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
import stat
import unicodedata
from pathlib import Path

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
MAX_SEED_FILE_CHARS = 4096


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


def read_seed_file(path: Path) -> str:
    """Read one private seed/passphrase line without exporting it to child processes."""
    try:
        with path.open(encoding="utf-8") as source:
            mode = os.fstat(source.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise SystemExit(f"seed file is not a regular file: {path}")
            if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
                raise SystemExit(f"seed file is readable by others; run: chmod 600 {path}")
            raw = source.read(MAX_SEED_FILE_CHARS + 1)
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"cannot read seed file {path}: {exc}") from exc
    if len(raw) > MAX_SEED_FILE_CHARS:
        raise SystemExit(f"seed file is over {MAX_SEED_FILE_CHARS} characters: {path}")
    given = raw.removesuffix("\n").removesuffix("\r")
    if not given or "\n" in given or "\r" in given:
        raise SystemExit(f"seed file must contain exactly one non-empty line: {path}")
    return given


def write_seed_file(path: Path, seed: str) -> None:
    """Create a private seed file without following or replacing an existing path."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise SystemExit(f"cannot create seed file {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
            destination.write(seed + "\n")
            destination.flush()
            os.fsync(destination.fileno())
    except OSError as exc:
        raise SystemExit(f"cannot write seed file {path}: {exc}") from exc


def load_key(seed_arg: str | None, seed_file: Path | None) -> tuple[Ed25519PrivateKey, str]:
    """The Ed25519 key for --seed-file / --seed / $SIGN_SEED, plus its provenance."""
    given = read_seed_file(seed_file) if seed_file is not None else seed_arg
    given = given or os.environ.get("SIGN_SEED")
    if given is None:
        raise SystemExit(
            "no key: pass --seed-file <path>, --seed <hex|passphrase>, or set $SIGN_SEED"
        )
    provenance = f"file:{seed_file}" if seed_file is not None else given
    if len(given) == 64:
        try:
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(given)), provenance
        except ValueError:
            pass  # 64 chars but not hex — fall through and hash it like any passphrase
    digest = hashlib.sha256(given.encode()).hexdigest()
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(digest)), f"sha256({provenance!r})"


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
    source = seeded.add_mutually_exclusive_group()
    source.add_argument(
        "--seed",
        default=argparse.SUPPRESS,
        help="64-hex-char seed, or any string (hashed with SHA-256)",
    )
    source.add_argument(
        "--seed-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="private file containing one seed/passphrase line; keygen creates it",
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
    if hasattr(args, "seed") and hasattr(args, "seed_file"):
        parser.error("--seed and --seed-file are mutually exclusive")

    if args.cmd == "keygen":
        seed = secrets.token_hex(32)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        seed_file = getattr(args, "seed_file", None)
        if seed_file is None:
            print(f"seed: {seed}")
        else:
            write_seed_file(seed_file, seed)
            print(f"seed file: {seed_file}")
        print(f"did:  {did_of(key)}")
        return

    seed = getattr(args, "seed", None)  # unset when --seed was passed nowhere (SUPPRESS)
    seed_file = getattr(args, "seed_file", None)
    if args.cmd == "did":
        key, _ = load_key(seed, seed_file)
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
    key, _ = load_key(seed, seed_file)
    print(did_of(key))
    print(signature(key, canonical))


if __name__ == "__main__":
    main()
