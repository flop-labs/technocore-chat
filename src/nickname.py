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
import time

import didkey
import store

# The DID-note directory convention (patterns.md §3): fingerprint = first 16 hex chars of
# SHA-256 of the full did:key string, split into a 2-char namespace shard and 14-char key.
def _note_ns_and_key(did: str) -> tuple[str, str]:
    fp = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    return f"did-{fp[:2]}", fp[2:]


class _NameCache:
    """Bounded, expiring resolve of did:key -> verified display name (or None).

    The store itself is the source of truth; this is only a bounded in-memory mirror so a
    50-message room read does not re-read + re-verify the same DID every request. Entries
    expire after a short TTL, and the note-write path calls :meth:`invalidate` so a just
    overwritten DID note is reflected on the *next* resolve instead of on some later
    eviction. A short-lived stale name is acceptable (the TTL is the generous /humans
    cache window for a name that would be verified again on the next read); a permanently
    stale one is not — which is exactly what expiry + write-path invalidation prevent.
    """

    __slots__ = ("_lock", "_cap", "_keys", "_map", "_ts", "_ttl", "_generation")

    def __init__(self, cap: int = 1024, ttl_ms: int = 30_000) -> None:
        self._lock = threading.Lock()
        self._cap = cap
        self._keys: list[str] = []
        self._map: dict[str, str | None] = {}
        self._ts: dict[str, float] = {}
        self._ttl = ttl_ms / 1000.0
        self._generation = 0

    def generation(self) -> int:
        """A counter bumped by every invalidate; see :meth:`put_if_current`."""
        with self._lock:
            return self._generation

    def get(self, did: str) -> str | None | bool:
        """The cached name, None when cached-unresolved, False when not cached/expired."""
        now = _monotonic()
        with self._lock:
            age = self._ts.get(did)
            if did in self._map and age is not None and (now - age) < self._ttl:
                return self._map[did]
            return False

    def put_if_current(self, did: str, name: str | None, generation: int) -> bool:
        """Publish a resolved name only if no invalidation happened since `generation`.

        Returns True when the entry was cached, False when a concurrent writer bumped the
        generation between the caller's read and this call — in which case `name` was
        derived from a pre-invalidation note and must NOT be cached, or an overwritten/lost
        name would live on in the cache past its TTL. The read returned this name exactly
        once; it just will not outlive this request.
        """
        with self._lock:
            if self._generation != generation:
                return False
            if did not in self._map:
                self._keys.append(did)
                if len(self._keys) > self._cap:
                    old = self._keys.pop(0)
                    self._map.pop(old, None)
                    self._ts.pop(old, None)
            self._map[did] = name
            self._ts[did] = _monotonic()
            return True

    def invalidate(self, did: str) -> None:
        """Drop any cached result for `did` so the next resolve re-reads the note."""
        with self._lock:
            self._map.pop(did, None)
            self._ts.pop(did, None)
            self._generation += 1

    def invalidate_all(self) -> None:
        """Drop every cached result (used when a DID-note namespace is rewritten).

        DID-note keys are content fingerprints, not reverse-mappable to a did:key, so a
        single overwrite cannot target one entry cheaply — but DID notes are written
        rarely, so clearing the whole (small) DID cache on any such write is simpler than
        a per-key scan and costs nothing in practice. Non-did namespaces are untouched.
        Bumping the generation also invalidates any in-flight read/parse/put that began
        before the write, closing the cache-reinsertion race (see put_if_current).
        """
        with self._lock:
            self._map.clear()
            self._ts.clear()
            self._keys.clear()
            self._generation += 1


def _monotonic() -> float:
    return time.monotonic()


_CACHE = _NameCache()


def invalidate(did: str) -> None:
    """Drop the cached result for `did`; call after the DID note is rewritten."""
    _CACHE.invalidate(did)


def invalidate_all_did_namespace() -> None:
    """Clear the DID cache when a DID-note namespace is written (see _NameCache)."""
    _CACHE.invalidate_all()


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
    # Capture the cache generation *before* the disk read: if a writer bumps it (via a
    # DID-note overwrite + invalidate_all_did_namespace) while we are reading/parsing,
    # the name we produce is derived from a pre-invalidation note and must not be cached,
    # or an erased/overwritten name would live on past its TTL (cache-reinsertion race).
    generation = _CACHE.generation()
    note = store.note_get(store_config_root(), ns, key)
    if note is None:
        # also try legacy single `did` namespace for pre-sharding identities
        note = store.note_get(store_config_root(), "did", _note_legacy_key(did))
    name = _parse_verified(note, did) if note is not None else None
    _CACHE.put_if_current(did, name, generation)
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