"""Solana Mobile MWA wallet-link proof verifier tests.

These are pure unit tests: no Starlette app or filesystem store is imported, so they are
runnable wherever Python + cryptography are available. The vertical HTTP integration comes only
after this verifier contract is green.
"""

from __future__ import annotations

import base64
import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

didkey = importlib.import_module("didkey")
wallet_link = importlib.import_module("wallet_link")


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
ORIGIN = "https://technocore.chat"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    """Minimal test-only Base58 encoder that preserves leading zero bytes."""
    zeros = len(raw) - len(raw.lstrip(b"\0"))
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, digit = divmod(value, 58)
        encoded = ALPHABET[digit] + encoded
    return "1" * zeros + encoded


def _did(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes_raw()
    return f"{didkey.PREFIX}z{_b58encode(didkey.MULTICODEC_ED25519 + raw)}"


def _challenge() -> str:
    return base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def _proof(
    *,
    did: str,
    wallet_key: Ed25519PrivateKey,
    issued_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> dict[str, str]:
    expires_at = expires_at or issued_at + timedelta(minutes=15)
    link = {
        "version": "technocore-chat-solana-mobile-wallet-link:v1",
        "origin": ORIGIN,
        "did": did,
        "wallet": _b58encode(wallet_key.public_key().public_bytes_raw()),
        "challenge": _challenge(),
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    link["signature"] = (
        base64.urlsafe_b64encode(wallet_key.sign(wallet_link.canonical_payload(link)))
        .decode()
        .rstrip("=")
    )
    return link


def _verify(link: dict[str, str], did: str) -> None:
    wallet_link.verify_wallet_link(link, did=did, configured_origin=ORIGIN, now=NOW)


def test_valid_mwa_wallet_link_proof_is_accepted_with_deterministic_bytes():
    did_key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
    wallet_key = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
    did = _did(did_key)
    link = _proof(did=did, wallet_key=wallet_key)

    assert wallet_link.canonical_payload(link) == (
        b"technocore-chat-solana-mobile-wallet-link:v1\n"
        b"origin:https://technocore.chat\n"
        + f"did:{did}\n".encode()
        + f"wallet:{link['wallet']}\n".encode()
        + b"challenge:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8\n"
        + b"issued_at:2026-08-19T12:00:00Z\n"
        + b"expires_at:2026-08-19T12:15:00Z\n"
    )
    _verify(link, did)


def test_proof_must_bind_exactly_to_the_existing_did():
    did = _did(Ed25519PrivateKey.from_private_bytes(b"\x01" * 32))
    other_did = _did(Ed25519PrivateKey.from_private_bytes(b"\x03" * 32))
    link = _proof(did=did, wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))

    with pytest.raises(wallet_link.WalletLinkError, match="DID"):
        _verify(link, other_did)


def test_proof_must_bind_to_the_configured_canonical_origin():
    did = _did(Ed25519PrivateKey.from_private_bytes(b"\x01" * 32))
    link = _proof(did=did, wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))
    link["origin"] = "https://evil.example"

    with pytest.raises(wallet_link.WalletLinkError, match="origin"):
        _verify(link, did)


def test_modified_canonical_field_invalidates_the_wallet_signature():
    did = _did(Ed25519PrivateKey.from_private_bytes(b"\x01" * 32))
    link = _proof(did=did, wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))
    link["challenge"] = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")

    with pytest.raises(wallet_link.WalletLinkError, match="signature"):
        _verify(link, did)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("wallet", "0OIl", "base58"),
        ("wallet", _b58encode(b"x" * 31), "32 bytes"),
        ("signature", "x", "signature"),
        ("challenge", "short", "challenge"),
        ("challenge", base64.urlsafe_b64encode(b"x" * 33).decode().rstrip("="), "challenge"),
        ("challenge", _challenge() + "=", "challenge"),
        ("origin", "https://technocore.chat\n.evil", "control"),
        ("did", "did:key:bad\nvalue", "control"),
    ],
)
def test_malformed_fields_fail_closed(field: str, value: str, message: str):
    did = _did(Ed25519PrivateKey.from_private_bytes(b"\x01" * 32))
    link = _proof(did=did, wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))
    link[field] = value

    with pytest.raises(wallet_link.WalletLinkError, match=message):
        _verify(link, did)


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    [
        (NOW, NOW, "after"),
        (NOW, NOW - timedelta(seconds=1), "after"),
        (NOW + timedelta(seconds=61), NOW + timedelta(minutes=10), "future"),
        (NOW, NOW + timedelta(minutes=15, seconds=1), "15 minutes"),
        (NOW - timedelta(minutes=16), NOW - timedelta(seconds=1), "expired"),
    ],
)
def test_time_bounds_fail_closed(issued_at: datetime, expires_at: datetime, message: str):
    did = _did(Ed25519PrivateKey.from_private_bytes(b"\x01" * 32))
    link = _proof(
        did=did,
        wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32),
        issued_at=issued_at,
        expires_at=expires_at,
    )

    with pytest.raises(wallet_link.WalletLinkError, match=message):
        _verify(link, did)


def test_unknown_or_missing_fields_are_rejected_before_verification():
    did = _did(Ed25519PrivateKey.from_private_bytes(b"\x01" * 32))
    link = _proof(did=did, wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))
    link["extra"] = "ambiguous"

    with pytest.raises(wallet_link.WalletLinkError, match="fields"):
        _verify(link, did)

    link = _proof(did=did, wallet_key=Ed25519PrivateKey.from_private_bytes(b"\x02" * 32))
    del link["signature"]
    with pytest.raises(wallet_link.WalletLinkError, match="fields"):
        _verify(link, did)


@pytest.mark.parametrize(
    ("configured_origin", "expected"),
    [
        ("https://technocore.chat/", "https://technocore.chat"),
        ("https://TECHNOCORE.chat", "https://technocore.chat"),
        ("http://localhost:8080", "http://localhost:8080"),
    ],
)
def test_configured_public_origin_is_normalized_without_using_request_headers(
    configured_origin: str, expected: str
):
    assert wallet_link.canonical_origin(configured_origin) == expected


@pytest.mark.parametrize(
    "configured_origin",
    [
        "",
        "technocore.chat",
        "https://technocore.chat/path",
        "https://user@technocore.chat",
        "https://technocore.chat?query=1",
        "https://technocore.chat#fragment",
        "ftp://technocore.chat",
    ],
)
def test_invalid_configured_origin_is_not_silently_derived_or_accepted(configured_origin: str):
    with pytest.raises(wallet_link.WalletLinkError, match="configured origin"):
        wallet_link.canonical_origin(configured_origin)
