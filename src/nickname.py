"""Nick-permanent resolution: map a signed did:key to its verified display name.

Implements the client-side convention from `patterns.md` (signed display name, PR #355)
on the server, so the render lane can *show* the name instead of only the abbreviated
key. The convention: a DID note may end in:

    nick:<name> sig:<base64url, unpadded>

where `sig` is an Ed25519 signature over the UTF-8 bytes of `<full did:key>|<name>`
(the full did:key string including its `did:key:` prefix, then `|`, then the name, no
whitespace). Verified against the key inside the DID itself; no room history needed.

The server resolves this so a reader gets the name, not an unkeyed promise: a note whose
sig is missing or does not verify names nobody. It never names the wrong key — an
overwrite can only erase a name, not steal one.

Resolution is deliberately a *cache first, disk second, verify-once* path. It runs in the
room-read thread, bounded by a small LRU so repeated renders do not re-read and
re-verify the same DID every request.
"""
from __future__ import annotations

import hashlib
import threading

import didkey
import store

# The DID-note directory convention (patterns.md §3): fingerprint = first 16 hex chars of
# SHA-256 of the full did:key string, split into a 2-char namespace shard and 14-char key.
def _note_ns_and_key(did: str) -> tuple[str, str]:
    fp = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    return f"did-{fp[:2]}", fp[2:]


class _NameCache:
    """Bounded resolve of did:key -> verified display name (or None).

    The store itself is the source of truth; this is only a bounded in-memory mirror so a
    50-message room read does not re-read + re-verify the same DID repeatedly. Entries are
    immutable (a DID note can be overwritten, but a reader is entitled to the name that was
    there when it looked; a TTL would re-verify on *every* read and defeat the point).
    """

    __slots__ = ("_lock", "_cap", "_keys", "_map")

    def __init__(self, cap: int = 1024) -> None:
        self._lock = threading.Lock()
        self._cap = cap
        self._keys: list[str] = []
        self._map: dict[str, str | None] = {}

    def get(self, did: str) -> str | None | bool:
        """The cached name, None when cached-unresolved, False when not cached."""
        with self._lock:
            if did in self._map:
                return self._map[did]
            return False

    def put(self, did: str, name: str | None) -> None:
        with self._lock:
            if did not in self._map:
                self._keys.append(did)
                if len(self._keys) > self._cap:
                    old = self._keys.pop(0)
                    self._map.pop(old, None)
            self._map[did] = name


_CACHE = _NameCache()


def lookahead_nick(did: str, discover: bool = False) -> str | None:
    """The verified display name for `did`, or None (fail-closed on any doubt).

    * discover=True  -> re-read the note (used by a fresh collector).
    * discover=False -> consult the bounded cache first, resolve from disk on a miss.
    """
    if not didkey.is_did(did):
        return None
    if not discover:
        cached: str | None | bool = _CACHE.get(did)
        if cached is not False:
            # cached name or cached-as-None (bool True is impossible; None is valid)
            return None if cached is True else cached
    ns, key = _note_ns_and_key(did)
    note = store.note_get(store_config_root(), ns, key)
    if note is None:
        # also try legacy single `did` namespace for pre-sharding identities
        note = store.note_get(store_config_root(), "did", _note_legacy_key(did))
    name = _parse_verified(note, did) if note is not None else None
    _CACHE.put(did, name)
    return name


def _parse_verified(note: str, did: str) -> str | None:
    """Return `name` when the note carries a `nick:` whose `sig:` verifies for `did`.

    Fail-closed: a missing/malformed/mis-verifying sig yields None, never a wrong name.
    """
    # trailing fields, whitespace-separated; the sig is the last field
    tokens = note.split()
    nick = sig = None
    for tok in reversed(tokens[-3:]):
        if tok.startswith("nick:") and nick is None:
            nick = tok[5:]
        elif tok.startswith("sig:") and sig is None:
            sig = tok[4:]
    if nick is None or sig is None:
        return None
    try:
        didkey.verify(did, sig, f"{did}|{nick}")
    except (didkey.DidError, didkey.SignatureError):
        return None
    return nick


# --- config/root indirection so tests can point at a scratch root ---
def store_config_root():  # overridable in tests
    import config

    return config.ROOT


def _note_legacy_key(did: str) -> str:
    """Legacy `did/<fingerprint>` key for identities published pre-sharding (#96)."""
    return hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]