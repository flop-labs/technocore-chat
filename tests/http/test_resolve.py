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