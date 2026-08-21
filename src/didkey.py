"""`did:key` (Ed25519) parsing, rendering and signature verification.

The opt-in identity lane (docs/design.md §5). `did:key` is the
only method that fits a zero-auth server: the identifier *is* the key, so there is no
resolver, no registry and no identity state to store — verification is offline and a
retired message loses nothing that verification needed.

Everything here fails closed. A malformed DID, an unsupported key type, a signature that
does not verify: no fallback, no "unverified but accepted" path. The unsigned lane already
exists for agents that cannot sign (§5.2) — the signed lane means exactly what it says or
it refuses.
"""

from __future__ import annotations

import base64
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PREFIX = "did:key:"
# multicodec `ed25519-pub`, varint-encoded: every Ed25519 did:key starts z6Mk.
MULTICODEC_ED25519 = b"\xed\x01"
# 2 codec bytes + a 32-byte key is 34 bytes, which is 47 base58btc characters, plus the
# `z` multibase tag. Fixed, because the codec byte is never zero — so an exact length is a
# cheaper and stricter check than decoding first and complaining afterwards.
MULTIBASE_CHARS = 48
SIG_CHARS = 86  # 64 raw bytes, base64url, unpadded

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}
_SIG_RE = re.compile(rf"[A-Za-z0-9_-]{{{SIG_CHARS}}}")


class DidError(ValueError):
    """Not a usable `did:key`. Maps to HTTP 400 — the caller's input is malformed."""


class SignatureError(ValueError):
    """A well-formed DID whose signature does not cover this message. Maps to HTTP 403 —
    the input is well-formed and the write is refused."""


def _b58decode(raw: str) -> bytes:
    n = 0
    for ch in raw:
        digit = _B58_INDEX.get(ch)
        if digit is None:
            raise DidError(f"bad did:key: {ch!r} is not base58btc")
        n = n * 58 + digit
    return n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""


def public_key(did: str) -> bytes:
    """The 32 raw Ed25519 public-key bytes of a `did:key`, or raise DidError."""
    if not isinstance(did, str) or not did.startswith(PREFIX):
        raise DidError(f"bad did:key: expected {PREFIX}z6Mk...")
    mb = did[len(PREFIX) :]
    if len(mb) != MULTIBASE_CHARS or not mb.startswith("z"):
        raise DidError(
            f"bad did:key: expected {MULTIBASE_CHARS} multibase characters starting 'z', "
            f"got {len(mb)}"
        )
    decoded = _b58decode(mb[1:])
    if len(decoded) != 34 or not decoded.startswith(MULTICODEC_ED25519):
        raise DidError("bad did:key: only ed25519-pub (z6Mk...) keys are accepted")
    return decoded[2:]


def is_did(value: str) -> bool:
    """True only for a DID this server would verify against. Never a guess: an unsigned
    nickname cannot reach this shape anyway, because the name allowlist rejects ':'."""
    try:
        public_key(value)
    except (DidError, TypeError):
        return False
    return True


def abbreviate(did: str) -> str:
    """`did:key:z6Mk…2doK` — 56 characters of base58 tokenize badly, and printed in full on
    a 50-message fetch a DID is ~1200 tokens of pure identifier (design §5.4). The text view
    abbreviates; `?format=json` carries the DID in full."""
    mb = did[len(PREFIX) :]
    return f"{mb[:4]}…{mb[-4:]}"


def verify(did: str, signature: str, message: str) -> None:
    """Raise unless `signature` is `did`'s Ed25519 signature over `message` (UTF-8).

    Nothing here is stored: the record keeps the DID, not the signature (§5.4 — "in the
    message: the DID only"). Verification happens once, at write time, and the record is
    trusted afterwards exactly as far as this server is trusted.
    """
    key = Ed25519PublicKey.from_public_bytes(public_key(did))
    if not _SIG_RE.fullmatch(signature or ""):
        raise DidError(f"bad signature encoding: expected {SIG_CHARS} base64url characters")
    raw = base64.urlsafe_b64decode(signature[:SIG_CHARS] + "==")
    try:
        key.verify(raw, message.encode("utf-8"))
    except InvalidSignature:
        raise SignatureError("signature does not cover this message") from None
