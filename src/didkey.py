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

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

PREFIX = "did:key:"
MULTICODEC_ED25519 = b"\xed\x01"
MULTIBASE_CHARS = 48
SIG_CHARS = 86

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

DID_PATTERN = rf"{PREFIX}z6Mk[1-9A-HJ-NP-Za-km-z]{{{MULTIBASE_CHARS - 4}}}"
SIG_PATTERN = rf"[A-Za-z0-9_-]{{{SIG_CHARS}}}"
NONCE_PATTERN = r"[0-9]{1,19}"

SIG_RE = re.compile(SIG_PATTERN)
NONCE_RE = re.compile(NONCE_PATTERN)


class DidError(ValueError):
    """Not a usable `did:key`. Maps to HTTP 400 — the caller's input is malformed."""


class SignatureError(ValueError):
    """A well-formed DID whose signature does not cover this message. Maps to HTTP 403."""


def _b58decode(raw: str) -> bytes:
    n = 0
    for ch in raw:
        digit = _B58_INDEX.get(ch)
        if digit is None:
            raise DidError(f"bad did:key: {ch!r} is not base58btc")
        n = n * 58 + digit

    # Base58btc represents each leading zero byte as a leading `1`. Integer
    # conversion alone loses those bytes, so restore them explicitly.
    leading_zeroes = len(raw) - len(raw.lstrip("1"))
    payload = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * leading_zeroes + payload


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
    try:
        public_key(value)
    except (DidError, TypeError):
        return False
    return True


def abbreviate(did: str) -> str:
    mb = did[len(PREFIX) :]
    return f"{mb[:4]}…{mb[-4:]}"


def verify(did: str, signature: str, message: str) -> None:
    key = VerifyKey(public_key(did))
    if not SIG_RE.fullmatch(signature or ""):
        raise DidError(f"bad signature encoding: expected {SIG_CHARS} base64url characters")
    raw = base64.urlsafe_b64decode(signature[:SIG_CHARS] + "==")
    try:
        key.verify(message.encode("utf-8"), raw)
    except BadSignatureError:
        raise SignatureError("signature does not cover this message") from None
