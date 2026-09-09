"""Run: uv run --group dev python -m pytest tests"""

import base64

import _client
import pytest
from _client import _keypair

client = _client.client  # the shared TestClient fixture
_get_note_ns = None  # resolved lazily to avoid import-order issues


def _did_note_path(client, did):
    """Put a DID note in the store so the resolver can read it. Returns its /kv URL."""
    import hashlib

    fp = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    ns, key = f"did-{fp[:2]}", fp[2:]
    return ns, key


def _publish_note(client, did, nick, sign_fn):
    """Write `<did> mail nick:<nick> sig:<b64>` at the did-<shard>/<key> note path."""
    ns, key = _did_note_path(client, did)
    sig = sign_fn(f"{did}|{nick}")
    value = f"{did} mailbox:mb-p-t x25519:AAAA nick:{nick} sig:{sig}"
    r = client.get(f"/kv/{ns}/{key}/set/{value.replace(' ', '%20')}")
    assert r.status_code == 200, r.text
    return ns, key


def test_resolve_404_when_no_note(client):
    did, _ = _keypair(1)
    r = client.get(f"/kv/resolve/{did}")
    assert r.status_code == 404
    assert "no verified name" in r.text


def test_resolve_400_for_non_did(client):
    r = client.get("/kv/resolve/notadid")
    assert r.status_code == 400


def test_resolve_returns_verified_nick(client):
    did, sign = _keypair(2)
    _publish_note(client, did, "Taufan", sign)
    r = client.get(f"/kv/resolve/{did}")
    assert r.status_code == 200
    assert r.text == "Taufan\n" or "Taufan" in r.text


def test_resolve_json(client):
    did, sign = _keypair(3)
    _publish_note(client, did, "taufan", sign)
    r = client.get(f"/kv/resolve/{did}?format=json")
    assert r.status_code == 200
    import json

    body = json.loads(r.text)
    assert body == {"did": did, "name": "taufan", "verified": True}


def test_resolve_fails_closed_on_wrong_signature(client):
    did, _ = _keypair(4)
    did2, sign2 = _keypair(5)  # different identity signs the name
    # note carries a sig from the WRONG key
    ns, key = _did_note_path(client, did)
    badsig = sign2(f"{did}|someone-else")
    value = f"{did} mailbox:mb-p-t x25519:AAAA nick:hacker sig:{badsig}"
    r = client.get(f"/kv/{ns}/{key}/set/{value.replace(' ', '%20')}")
    assert r.status_code == 200
    rr = client.get(f"/kv/resolve/{did}")
    assert rr.status_code == 404  # no verified name
    assert "no verified name" in rr.text


def test_resolve_nick_missing_sig_is_unresolved(client):
    did, _ = _keypair(6)
    ns, key = _did_note_path(client, did)
    value = f"{did} mailbox:mb-p-t x25519:AAAA nick:anon"  # no sig
    r = client.get(f"/kv/{ns}/{key}/set/{value.replace(' ', '%20')}")
    assert r.status_code == 200
    rr = client.get(f"/kv/resolve/{did}")
    assert rr.status_code == 404


def test_resolve_uses_legacy_did_namespace(client, monkeypatch):
    """Pre-sharding identities lived at /kv/did/<fingerprint>; still resolve them."""
    import hashlib

    did, sign = _keypair(7)
    fp = hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]
    sig = sign(f"{did}|legacy")
    value = f"{did} mailbox:mb-p-t x25519:AAAA nick:legacy sig:{sig}"
    r = client.get(f"/kv/did/{fp}/set/{value.replace(' ', '%20')}")
    assert r.status_code == 200
    rr = client.get(f"/kv/resolve/{did}")
    assert rr.status_code == 200
    assert "legacy" in rr.text


def test_resolve_reflects_note_overwrite(client):
    """The cache must not keep a resolved name alive after its DID note is rewritten.

    Regression for the reviewer note: an entry cached as immutable without expiry would
    keep serving `alice` after the note was overwritten to `bob` (or invalidated), breaking
    the fail-closed guarantee. The note-write path invalidates the DID cache, so the very
    next resolve re-reads the note and reflects the new/invalid state.
    """
    did, sign = _keypair(10)
    ns, key = _did_note_path(client, did)

    def set_note(value):
        r = client.get(f"/kv/{ns}/{key}/set/{value.replace(' ', '%20')}")
        assert r.status_code == 200, r.text

    # 1. publish alice, resolve it (primes the cache)
    set_note(f"{did} mailbox:mb-p-t x25519:AAAA nick:alice sig:{sign(f'{did}|alice')}")
    r1 = client.get(f"/kv/resolve/{did}")
    assert r1.status_code == 200 and "alice" in r1.text

    # 2. overwrite to a different valid name (valid sig)
    set_note(f"{did} mailbox:mb-p-t x25519:AAAA nick:bob sig:{sign(f'{did}|bob')}")
    r2 = client.get(f"/kv/resolve/{did}")
    assert r2.status_code == 200, f"expected 200 got {r2.status_code}: {r2.text}"
    assert "bob" in r2.text, f"expected bob, got {r2.text!r}"

    # 3. overwrite again, dropping nick:/sig: entirely -> must fail closed
    set_note(f"{did} mailbox:mb-p-t x25519:AAAA")
    r3 = client.get(f"/kv/resolve/{did}")
    assert r3.status_code == 404, f"expected 404 got {r3.status_code}: {r3.text}"


def test_resolve_cache_validated_against_shared_store_other_worker(client):
    """Regression: worker B overwriting a note invalidates worker A's cache next resolve.

    The cache must be validated against *shared-store state* (the note's mtime), not a
    process-local invalidation — a second worker cannot know to clear another worker's
    `_NameCache`. Two independent cache instances share the same store: A primes `alice`,
    B overwrites the note to `bob`, and A's very next resolve must return `bob`, not the
    pre-write `alice` it still holds in memory.
    """
    import nickname
    import store
    import config

    did, sign = _keypair(11)
    ns, key = _did_note_path(client, did)

    def write_note(value):
        r = client.get(f"/kv/{ns}/{key}/set/{value.replace(' ', '%20')}")
        assert r.status_code == 200, r.text

    # cache A primes alice against the shared store
    cache_a = nickname._NameCache()
    orig = nickname._CACHE
    nickname._CACHE = cache_a
    try:
        write_note(f"{did} mailbox:mb-p-t x25519:AAAA nick:alice sig:{sign(f'{did}|alice')}")
        assert nickname.lookahead_nick(did) == "alice"

        # worker B (a *different* cache instance, sharing the same store) overwrites to bob
        cache_b = nickname._NameCache()
        nickname._CACHE = cache_b
        write_note(f"{did} mailbox:mb-p-t x25519:AAAA nick:bob sig:{sign(f'{did}|bob')}")

        # return to worker A: its cache still holds alice, but the shared note changed,
        # so its next resolve re-reads and returns bob rather than the pre-write name.
        nickname._CACHE = cache_a
        got = nickname.lookahead_nick(did)
        assert got == "bob", f"cache A must reflect the other worker's write, got {got!r}"

        # and the cache was updated, not left stale
        name, mt = cache_a.get(did)
        assert name == "bob"
        assert mt == nickname._note_mtime_ns(config.ROOT, ns, key)
    finally:
        nickname._CACHE = orig