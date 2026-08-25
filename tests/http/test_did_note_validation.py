"""Regression tests for DID-note write validation (issue #199).

The `did` / `did-<shard>` namespaces are at a hard note cap, so a write that
cannot answer the lookup those namespaces exist for — a value with no did:key,
or one whose fingerprint does not match the slot — must be refused on write
rather than left to fill a slot a working identity cannot have.

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


def _did_and_fp() -> tuple[str, str]:
    import didkey

    did = "did:key:z6MkmzyBxvrSZveZv5YhZhfwUYQYv5LDgt5NuqVrBe5vXvPA"
    assert didkey.is_did(did)
    return did, hashlib.sha256(did.encode()).hexdigest()[:16]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as app_module
    import config

    app_module._buckets.clear()
    with config.override(ROOT=tmp_path):
        yield TestClient(app_module.app)


def test_valid_legacy_did_note_accepted(client):
    did, fp = _did_and_fp()
    resp = client.get(f"/kv/did/{fp}/set/{did}%20|%20note:regression-test")
    assert resp.status_code == 200, resp.text
    assert resp.text.startswith("ok did/")


def test_valid_sharded_did_note_accepted(client):
    did, fp = _did_and_fp()
    shard, key = fp[:2], fp[2:]
    resp = client.get(f"/kv/did-{shard}/{key}/set/{did}%20|%20note:sharded")
    assert resp.status_code == 200, resp.text
    assert resp.text.startswith(f"ok did-{shard}/")


def test_value_without_did_rejected(client):
    did, fp = _did_and_fp()
    resp = client.get(f"/kv/did/{fp}/set/agent:xiuxiu-073%20active")
    assert resp.status_code == 400, resp.text
    assert "did:key" in resp.text


def test_wrong_slot_did_rejected(client):
    did, fp = _did_and_fp()
    # A well-formed DID written to a slot whose key is NOT its fingerprint.
    wrong_slot = "f" * 16
    resp = client.get(f"/kv/did/{wrong_slot}/set/{did}%20|%20note:wrong-slot")
    assert resp.status_code == 400, resp.text
    assert "fingerprint" in resp.text


def test_non_did_namespace_unaffected(client):
    # The gate must not touch ordinary world-writable namespaces.
    resp = client.get("/kv/p-abc123/state/set/some%20session%20state")
    assert resp.status_code == 200, resp.text
