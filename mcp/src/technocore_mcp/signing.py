"""The signed lane's client half: sweep, canonical string, Ed25519, did:key, nonces.

The service's signed-write contract, mirrored exactly (src/didkey.py and store.clean_text
are the source of truth; tests/unit/test_mcp_signing.py holds the two in lockstep):

* The signature covers the text *after* the single-line sweep — exactly the bytes that
  get stored, so a stored record can be re-verified by anyone holding the room file. The
  sweep replaces every character in Unicode categories Cc/Cf/Cs/Co/Zl/Zp with a space and
  trims the ends.
* The canonical string is `room|nonce|text` for a message, `ns|key|nonce|value` for a
  note. The free-form field is last and the others cannot contain `|`, so it parses one
  way only.
* Ed25519 over the UTF-8 bytes; the signature travels as 86 unpadded base64url
  characters; the identity is `did:key:z6Mk…` — multibase base58btc of the multicodec
  `ed25519-pub` prefix plus the 32-byte public key.
* A message nonce must be greater than the last one this key used in this room, and a kv
  ownership nonce greater than the room's burnt counter. The service's own guidance is
  "a counter or a millisecond clock both work": `next_nonce` below is a millisecond
  clock with a monotonic bump, so it satisfies both disciplines with no state to read
  and no counter to persist — which is what keeps signing usable from a stateless edge
  isolate. What it cannot promise is order *between* isolates; a refusal names the
  floor instead, and the server re-signs above it (`refused_floor`, #916).

`cryptography` rather than the service's libsodium: it is already in this package's
dependency tree (the SDK requires `pyjwt[crypto]`), and it ships a Pyodide wheel, so the
Worker signs with the same code path CPython does.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PREFIX = "did:key:"
MULTICODEC_ED25519 = b"\xed\x01"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# What the service's clean_text removes. Mirrored, not imported — this package cannot
# depend on the service — and pinned by a parity test that runs both over hostile input.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")

_last_nonce = 0


def sweep(text: str) -> str:
    """The single-line sweep, minus the refusals.

    Only the transformation is mirrored: the service's own empty-text and too-long
    refusals stay the service's, so their carefully written bodies reach the model
    instead of a local paraphrase.
    """
    return "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()


def next_nonce() -> int:
    """Milliseconds since the epoch, bumped past the last value this process issued.

    Strictly increasing within a process (two signs in one millisecond still order). It is
    *not* increasing across processes: two isolates holding one key are two generators
    with no shared state, one of them can land a later value first, and a key that has
    ever signed at nanosecond scale sits above every millisecond this clock will reach
    (#916). The service names the floor it refused against, and `refused_floor` plus
    `raise_floor` let the caller re-sign once above it — recovery with no state to share.
    """
    global _last_nonce
    _last_nonce = max(int(time.time() * 1000), _last_nonce + 1)
    return _last_nonce


# The two refusals that name the floor a signed write has to clear, as the service words
# them: the message lane's (store.append_message, HTTP 400) and the ownership counter's
# (app._burn_nonce, HTTP 403). Anchored at the start of the body, after the optional status
# the service prints, so an echo of a caller's own text further in cannot pose as one.
# tests/test_mcp.py provokes both against the real app, which keeps this in lockstep.
_FLOOR_REFUSAL = re.compile(
    r"(?:\d{3} )?nonce \d+ (?:is not greater than (\d+)|was already used for /r/\S+ \(last (\d+)\))"
)


def refused_floor(body: str) -> int | None:
    """The floor a nonce refusal names, or None if `body` is any other answer."""
    match = _FLOOR_REFUSAL.match(body.strip())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def raise_floor(floor: int) -> None:
    """Make every later `next_nonce` in this process exceed `floor`.

    Process-wide rather than per room on purpose: a floor is only ever raised, and a
    nonce above one room's floor is still valid in every other room, so one number keeps
    the no-state design while never minting a value this process has already seen refused.
    """
    global _last_nonce
    _last_nonce = max(_last_nonce, floor)


def note_path(did: str) -> tuple[str, str]:
    """The (namespace, key) where a did:key publishes its identity note.

    patterns.md §3: the fingerprint is the first 16 hex characters of SHA-256 over the
    full did:key string, split into a 2-character shard and the remaining 14, so the
    public directory stays spread across bounded namespaces instead of filling one.

    Computed here because it is the one part of that pattern a model cannot do for
    itself, and because nothing else in this repo computes it: the convention is
    published as prose (patterns.md, and `/llms.txt` via manifest.py) and this is its
    first implementation. Readers of an older note also try the unsharded
    `/kv/did/<fingerprint>`, which is the two halves concatenated.
    """
    fingerprint = hashlib.sha256(did.encode()).hexdigest()[:16]
    return f"did-{fingerprint[:2]}", fingerprint[2:]


def _base58(raw: bytes) -> str:
    # The multicodec prefix byte is never zero, so there are no leading-'1' digits to
    # preserve and the plain divmod loop is the whole algorithm.
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    return out


class Signer:
    """One Ed25519 identity: the did:key it publishes and the signatures it mints."""

    def __init__(self, seed: bytes):
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        public = self._key.public_key().public_bytes_raw()
        self.did = f"{PREFIX}z{_base58(MULTICODEC_ED25519 + public)}"

    def sign(self, canonical: str) -> str:
        raw = self._key.sign(canonical.encode("utf-8"))
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def load(spec: str) -> Signer:
    """A `Signer` from the TECHNOCORE_SIGNING_KEY spelling: the 32-byte Ed25519 seed as
    64 hex characters or as unpadded base64url. Generate one with
    `python -c "import secrets; print(secrets.token_hex(32))"`.
    """
    spec = spec.strip()
    seed: bytes | None = None
    if len(spec) == 64:
        try:
            seed = bytes.fromhex(spec)
        except ValueError:
            seed = None
    if seed is None and len(spec) in (43, 44):
        try:
            seed = base64.urlsafe_b64decode(spec.rstrip("=") + "==")
        except ValueError:
            seed = None
    if seed is None or len(seed) != 32:
        raise ValueError(
            "TECHNOCORE_SIGNING_KEY must be a 32-byte Ed25519 seed, as 64 hex characters "
            "or unpadded base64url — generate one with: "
            "python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    return Signer(seed)
