"""Regression tests for DID-note write validation (issue #199).

The `did` / `did-<shard>` namespaces are at a hard cap, so a write that cannot
answer the lookup those namespaces exist for — a value with no did:key, or one
whose fingerprint does not match the slot — must be refused on write rather than
left to fill a slot a working identity cannot have.

Run: uv run --group dev python -m pytest tests/http/test_did_note_validation.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _keypair(seed: int = 1):
    """Deterministic Ed25519 did:key (from tests/_client.py, copied to avoid the
    import-as-fixture pitfall)."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import didkey

    key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    raw = key.public_key().public_bytes_raw()

    def sign(message: str) -> str:
        return base64.urlsafe_b64encode(key.sign(message.encode())).decode().rstrip("=")

    return f"{didkey.PREFIX}z{_multibase(didkey.MULTICODEC_ED25519 + raw)}", sign


def _multibase(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out
    return out


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as app_module
    import config

    app_module._buckets.clear()
    app_module._rooms_cache.clear()
    app_module._identities.clear()
    app_module._proxy_evidence["proxied_requests"] = 0
    with config.override(ROOT=tmp_path):
        yield TestClient(app_module.app)


def _fp(did: str) -> str:
    return hashlib.sha256(did.encode()).hexdigest()[:16]


def test_valid_legacy_did_note_accepted(client):
    did, _ = _keypair(1)
    fp = _fp(did)
    resp = client.get(f"/kv/did/{fp}/set/{did}%20|%20note:regression-test")
    assert resp.status_code == 200, resp.text
    assert resp.text.startswith("ok did/")


def test_valid_sharded_did_note_accepted(client):
    did, _ = _keypair(2)
    fp = _fp(did)
    shard, key = fp[:2], fp[2:]
    resp = client.get(f"/kv/did-{shard}/{key}/set/{did}%20|%20note:sharded")
    assert resp.status_code == 200, resp.text
    assert resp.text.startswith(f"ok did-{shard}/")


def test_value_without_did_rejected(client):
    did, _ = _keypair(3)
    fp = _fp(did)
    resp = client.get(f"/kv/did/{fp}/set/agent:xiuxiu-073%20active")
    assert resp.status_code == 400, resp.text
    assert "did:key" in resp.text
    assert "p-<random>/state" in resp.text


def test_wrong_slot_did_rejected(client):
    did, _ = _keypair(4)
    wrong_slot = "f" * 16
    resp = client.get(f"/kv/did/{wrong_slot}/set/{did}%20|%20note:wrong-slot")
    assert resp.status_code == 400, resp.text
    assert "fingerprints to /kv/did-" in resp.text


def test_non_shard_prefix_stays_world_writable(client):
    resp = client.get("/kv/did-registry/anything/set/no-did-here")
    assert resp.status_code == 200, resp.text
    assert resp.text.startswith("ok did-registry/")


def test_legacy_slot_must_be_16hex(client):
    did, _ = _keypair(5)
    resp = client.get(f"/kv/did/alice/set/{did}%20|%20note:bad-slot")
    assert resp.status_code == 400, resp.text
    assert "16-hex" in resp.text


def test_signed_write_to_did_gets_lane_refusal_not_did_message(client):
    did, sign = _keypair(6)
    fp = _fp(did)
    resp = client.get(f"/kv/did/{fp}/set-signed/{did}/{sign(f'did|{fp}|1|{did}')}/1/{did}")
    assert resp.status_code == 400, resp.text
    assert "signed note writes are only accepted for" in resp.text
