"""HTTP integration tests for optional Solana Mobile MWA wallet-link proofs on signed POST."""

from __future__ import annotations

import base64
import importlib
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient

# The upstream store deliberately uses POSIX flock. This no-op shim exists only so this focused
# HTTP slice can run on Windows; Linux CI imports real fcntl and exercises the real lock.
try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    shim: Any = types.ModuleType("fcntl")
    shim.LOCK_EX = 1
    shim.LOCK_UN = 2
    shim.flock = lambda *_args: None
    sys.modules["fcntl"] = shim

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
didkey = importlib.import_module("didkey")
wallet_link = importlib.import_module("wallet_link")

NOW = datetime.now(UTC).replace(microsecond=0)
ORIGIN = "https://technocore.chat"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    zeros = len(raw) - len(raw.lstrip(b"\0"))
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, digit = divmod(value, 58)
        encoded = ALPHABET[digit] + encoded
    return "1" * zeros + encoded


def _did(key: Ed25519PrivateKey) -> str:
    return f"{didkey.PREFIX}z{_b58encode(didkey.MULTICODEC_ED25519 + key.public_key().public_bytes_raw())}"


def _signature(key: Ed25519PrivateKey, message: str) -> str:
    return base64.urlsafe_b64encode(key.sign(message.encode())).decode().rstrip("=")


def _wallet_link(did: str, wallet_key: Ed25519PrivateKey) -> dict[str, str]:
    link = {
        "version": wallet_link.VERSION,
        "origin": ORIGIN,
        "did": did,
        "wallet": _b58encode(wallet_key.public_key().public_bytes_raw()),
        "challenge": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "issued_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (NOW + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    link["signature"] = (
        base64.urlsafe_b64encode(wallet_key.sign(wallet_link.canonical_payload(link)))
        .decode()
        .rstrip("=")
    )
    return link


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    def build(public_url: str | None = ORIGIN) -> TestClient:
        monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
        if public_url is None:
            monkeypatch.delenv("CHAT_PUBLIC_URL", raising=False)
        else:
            monkeypatch.setenv("CHAT_PUBLIC_URL", public_url)
        for module in ("app", "manifest", "store"):
            sys.modules.pop(module, None)
        app_module = importlib.import_module("app")
        return TestClient(app_module.app)

    return build


def _signed_post(client: TestClient, key: Ed25519PrivateKey, text: str, nonce: int, **extra):
    did = _did(key)
    return client.post(
        "/r/lobby?format=json",
        json={
            "did": did,
            "sig": _signature(key, f"lobby|{nonce}|{text}"),
            "nonce": str(nonce),
            "text": text,
            **extra,
        },
    )


def test_existing_signed_post_without_wallet_link_is_unchanged(make_client):
    client = make_client()
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)

    response = _signed_post(client, did_key, "existing signed post", 1)

    assert response.status_code == 200
    posted = response.json()["posted"]
    assert posted == {
        "seq": 1,
        "ts": posted["ts"],
        "from": _did(did_key),
        "text": "existing signed post",
        "nonce": 1,
    }
    assert "solana_wallet_link" not in posted


def test_valid_solana_mobile_mwa_wallet_link_is_accepted_without_persistence(make_client):
    client = make_client()
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    proof = _wallet_link(_did(did_key), Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))

    response = _signed_post(client, did_key, "MWA proof attached", 1, solana_wallet_link=proof)

    assert response.status_code == 200
    posted = response.json()["posted"]
    assert posted["from"] == _did(did_key)
    assert posted["text"] == "MWA proof attached"
    assert "solana_wallet_link" not in posted and "wallet" not in posted


def test_invalid_wallet_link_is_rejected_after_the_did_signature_is_valid(make_client):
    client = make_client()
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    proof = _wallet_link(_did(did_key), Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))
    proof["signature"] = "A" * 86

    response = _signed_post(client, did_key, "invalid MWA proof", 1, solana_wallet_link=proof)

    assert response.status_code == 400
    assert "wallet-link signature" in response.text
    assert client.get("/r/lobby?format=json").json()["count"] == 0


def test_wallet_link_did_must_equal_the_signed_post_did(make_client):
    client = make_client()
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    other_did_key = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
    proof = _wallet_link(_did(other_did_key), Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))

    response = _signed_post(client, did_key, "wrong DID proof", 1, solana_wallet_link=proof)

    assert response.status_code == 400
    assert "does not match the signed write DID" in response.text


def test_proof_is_rejected_when_chat_public_url_is_not_explicitly_configured(make_client):
    client = make_client(public_url=None)
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    proof = _wallet_link(_did(did_key), Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))

    response = _signed_post(
        client, did_key, "requires configured origin", 1, solana_wallet_link=proof
    )

    assert response.status_code == 400
    assert "CHAT_PUBLIC_URL" in response.text


def test_anonymous_signed_get_and_signed_post_flows_remain_unaffected(make_client):
    client = make_client(public_url=None)
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    did = _did(did_key)

    anonymous = client.post("/r/anonymous", json={"from": "bot", "text": "plain post"})
    signed_post = _signed_post(client, did_key, "plain signed post", 1)
    signed_get = client.get(
        f"/r/get-room/say-signed/{did}/{_signature(did_key, 'get-room|1|signed GET')}/1/signed GET"
    )

    assert anonymous.status_code == 200
    assert signed_post.status_code == 200
    assert signed_get.status_code == 200
    assert client.get("/r/anonymous?format=json").json()["messages"][0]["from"] == "bot"
    assert client.get("/r/lobby?format=json").json()["messages"][0]["from"] == did
    assert client.get("/r/get-room?format=json").json()["messages"][0]["from"] == did


def test_openapi_and_served_manual_document_the_optional_mwa_client_extension(make_client):
    client = make_client()
    schema = client.get("/openapi.json").json()["paths"]["/r/{room}"]["post"]["requestBody"]
    properties = schema["content"]["application/json"]["schema"]["properties"]

    assert "solana_wallet_link" in properties
    assert "Solana Mobile" in properties["solana_wallet_link"]["description"]
    manual = client.get("/llms.txt").text
    assert "Solana Mobile MWA client wallet-link proof" in manual
    assert "not Seeker or device attestation" in manual
