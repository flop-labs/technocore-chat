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
from typing import cast

import didkey
import store

# The DID-note directory convention (patterns.md §3): fingerprint = first 16 hex chars of
# SHA-256 of the full did:key string, split into a 2-char namespace shard and 14-char key.
def _note_ns_and_key(did: str) -> tuple[str, str]:
    fp = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    return f"did-{fp[:2]}", fp[2:]


class _NameCache:
    """Bounded, store-validated resolve of did:key -> verified display name (or None).

    The store itself is the source of truth, not this process's memory. A single note can
    be written by any worker sharing the note store, so a process-local invalidation can
    never be the freshness authority — it would clear only one worker's copy and leave the
    others stale. Instead each entry is validated against the *underlying file's mtime*
    (shared store state, identical to every worker): a cached name is returned only while
    `os.stat(note_path).st_mtime_ns` still matches what was cached. Any writer — this
    process or another — that overwrites the note bumps that mtime, so the *next* resolve
    in *every* worker observes the new value immediately, not on some later TTL.
    """

    __slots__ = ("_lock", "_cap", "_keys", "_map", "_mtime")

    def __init__(self, cap: int = 1024) -> None:
        self._lock = threading.Lock()
        self._cap = cap
        self._keys: list[str] = []
        self._map: dict[str, str | None] = {}
        self._mtime: dict[str, int] = {}

    def get(self, did: str) -> tuple[str | None | bool, int | None]:
        """The cached (name, note_mtime) pair, or (False, None) when not cached."""
        with self._lock:
            if did in self._map:
                return self._map[did], self._mtime[did]
            return False, None

    def put(self, did: str, name: str | None, mtime: int) -> None:
        """Cache `name` as the note's verified name while the note keeps `mtime`."""
        with self._lock:
            if did not in self._map:
                self._keys.append(did)
                if len(self._keys) > self._cap:
                    old = self._keys.pop(0)
                    self._map.pop(old, None)
                    self._mtime.pop(old, None)
            self._map[did] = name
            self._mtime[did] = mtime


def _monotonic() -> float:
    return time.monotonic()


_CACHE = _NameCache()


def _note_mtime_ns(root, ns: str, key: str) -> int | None:
    """The note file's mtime in nanoseconds, or None when the note does not exist.

    This is *shared-store state*: every worker resolving the same did:key stats the same
    underlying file, so a writer in another process bumps this value and every worker's
    cache sees it on the next resolve. It is the freshness authority the cache validates
    against — deliberately not a process-local invalidation, which could not see writes
    from another worker.
    """
    try:
        return store.note_path(root, ns, key).stat().st_mtime_ns
    except OSError:
        return None


def lookahead_nick(did: str, discover: bool = False) -> str | None:
    """The verified display name for `did`, or None (fail-closed on any doubt).

    * discover=True  -> re-read the note, ignoring the cache.
    * discover=False -> validate the cache against the note's live mtime, resolve on a miss.

    Cache validity is keyed on the note's on-disk mtime (shared store state), so an
    overwrite by *any* worker is observed on the very next resolve — not just one whose
    process-local cache happened to be invalidated.
    """
    if not didkey.is_did(did):
        return None
    ns, key = _note_ns_and_key(did)
    root = store_config_root()

    # The mtime of whichever note actually backs this identity (sharded, then legacy).
    def backing_mtime() -> int | None:
        m = _note_mtime_ns(root, ns, key)
        if m is None:
            m = _note_mtime_ns(root, "did", _note_legacy_key(did))
        return m

    if not discover:
        cached, cached_mtime = _CACHE.get(did)
        if cached is not False and cached_mtime is not None:
            # valid only while the shared note still has the mtime we cached under
            if backing_mtime() == cached_mtime:
                # cached name or cached-as-None (the None case is cached and current)
                return cast("str | None", cached)
            # note changed under us (this or another worker) -> fall through to re-read

    note = store.note_get(root, ns, key)
    if note is None:
        note = store.note_get(root, "did", _note_legacy_key(did))
    name = _parse_verified(note, did) if note is not None else None
    _CACHE.put(did, name, backing_mtime() or 0)
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