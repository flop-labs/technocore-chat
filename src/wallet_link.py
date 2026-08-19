"""Solana Mobile MWA client wallet-link proof verification.

This optional extension is designed for Solana Mobile clients using Mobile Wallet Adapter (MWA)
beside an existing Technocore `did:key` signed POST. The server cryptographically verifies only
control of the presented Solana Ed25519 public key over this exact short-lived binding statement.
It does not persist anything, authorize anything, establish an account, or attest to MWA, Seeker,
or a device.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import didkey

VERSION = "technocore-chat-solana-mobile-wallet-link:v1"
MAX_VALIDITY = timedelta(minutes=15)
MAX_FUTURE_SKEW = timedelta(seconds=60)
WALLET_BYTES = 32
SIGNATURE_BYTES = 64
CHALLENGE_BYTES = 32

_FIELDS = (
    "version",
    "origin",
    "did",
    "wallet",
    "challenge",
    "issued_at",
    "expires_at",
    "signature",
)
_PAYLOAD_FIELDS = _FIELDS[:-1]
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58)}
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class WalletLinkError(ValueError):
    """A wallet-link proof is malformed, out of scope, expired, or unverifiable."""


def _reject_invisible(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise WalletLinkError(f"{field} must be a string")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co") for char in value):
        raise WalletLinkError(f"{field} contains a control or format character")
    return value


def _fields(link: Mapping[str, str], required: tuple[str, ...]) -> None:
    if not isinstance(link, Mapping) or set(link) != set(required):
        raise WalletLinkError("wallet-link fields must be exactly the v1 fields")
    for field in required:
        _reject_invisible(link[field], field)


def _b58decode(value: str) -> bytes:
    if not value or len(value) > 44:
        raise WalletLinkError("wallet must be a compact base58 public key")
    leading_zeros = len(value) - len(value.lstrip("1"))
    number = 0
    for char in value:
        digit = _B58_INDEX.get(char)
        if digit is None:
            raise WalletLinkError("wallet must be base58")
        number = number * 58 + digit
    encoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * leading_zeros + encoded


def _base64url(value: str, *, field: str, expected_bytes: int) -> bytes:
    if not _B64URL_RE.fullmatch(value or ""):
        raise WalletLinkError(f"{field} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise WalletLinkError(f"{field} must be unpadded base64url") from exc
    if len(decoded) != expected_bytes:
        raise WalletLinkError(f"{field} has the wrong length")
    return decoded


def _timestamp(value: str, *, field: str) -> datetime:
    if not _TIMESTAMP_RE.fullmatch(value):
        raise WalletLinkError(f"{field} must be UTC RFC3339 seconds ending in Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise WalletLinkError(f"{field} must be UTC RFC3339 seconds ending in Z") from exc


def canonical_origin(configured_origin: str) -> str:
    """Normalize only an explicitly configured public origin, never a request Host header."""
    if not isinstance(configured_origin, str) or not configured_origin:
        raise WalletLinkError("configured origin must be an explicit http(s) origin")
    parts = urlsplit(configured_origin)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise WalletLinkError("configured origin must be an explicit http(s) origin")
    try:
        port = parts.port
    except ValueError as exc:
        raise WalletLinkError("configured origin must be an explicit http(s) origin") from exc
    host = parts.hostname.lower()
    authority = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), authority, "", "", ""))


def canonical_payload(link: Mapping[str, str]) -> bytes:
    """Return the exact versioned UTF-8 bytes a Solana wallet key signs for v1."""
    fields = _FIELDS if set(link) == set(_FIELDS) else _PAYLOAD_FIELDS
    _fields(link, fields)
    if link["version"] != VERSION:
        raise WalletLinkError(f"unsupported wallet-link version {link['version']!r}")
    return (
        VERSION + "\n" + "\n".join(f"{field}:{link[field]}" for field in _PAYLOAD_FIELDS[1:]) + "\n"
    ).encode("utf-8")


def verify_wallet_link(
    link: Mapping[str, str], *, did: str, configured_origin: str, now: datetime | None = None
) -> None:
    """Validate one Solana Mobile MWA client wallet-link proof, or raise WalletLinkError.

    Validation cryptographically checks only Solana Ed25519 key control. `configured_origin` must
    come from an operator setting such as CHAT_PUBLIC_URL. The verifier deliberately has no request
    object, so arbitrary Host headers cannot become proof authority.
    """
    _fields(link, _FIELDS)
    payload = canonical_payload(link)
    allowed_origin = canonical_origin(configured_origin)
    if link["origin"] != allowed_origin:
        raise WalletLinkError("wallet-link origin does not match the configured canonical origin")
    if link["did"] != did:
        raise WalletLinkError("wallet-link DID does not match the signed write DID")
    try:
        didkey.public_key(did)
    except didkey.DidError as exc:
        raise WalletLinkError(f"wallet-link DID is invalid: {exc}") from exc

    wallet = _b58decode(link["wallet"])
    if len(wallet) != WALLET_BYTES:
        raise WalletLinkError("wallet public key must decode to exactly 32 bytes")
    _base64url(link["challenge"], field="challenge", expected_bytes=CHALLENGE_BYTES)
    signature = _base64url(link["signature"], field="signature", expected_bytes=SIGNATURE_BYTES)

    issued_at = _timestamp(link["issued_at"], field="issued_at")
    expires_at = _timestamp(link["expires_at"], field="expires_at")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise WalletLinkError("verification time must be timezone-aware")
    now = now.astimezone(UTC)
    if expires_at <= issued_at:
        raise WalletLinkError("expires_at must be after issued_at")
    if expires_at < now:
        raise WalletLinkError("wallet-link proof is expired")
    if issued_at > now + MAX_FUTURE_SKEW:
        raise WalletLinkError("issued_at is unreasonably far in the future")
    if expires_at - issued_at > MAX_VALIDITY:
        raise WalletLinkError("wallet-link validity may not exceed 15 minutes")

    try:
        Ed25519PublicKey.from_public_bytes(wallet).verify(signature, payload)
    except InvalidSignature:
        raise WalletLinkError(
            "wallet-link signature does not cover the canonical payload"
        ) from None
