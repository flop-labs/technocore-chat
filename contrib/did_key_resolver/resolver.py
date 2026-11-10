"""Expand an Ed25519 did:key into a minimal public DID document."""

from __future__ import annotations

from nacl.bindings import crypto_core_ed25519_is_valid_point

from didkey import DidError, public_key


class DidResolutionError(ValueError):
    """The identifier cannot be resolved by this Ed25519-only resolver."""


def resolve(did: str) -> dict:
    """Return an offline DID document, or raise DidResolutionError.

    Only bare, base58btc-encoded Ed25519 did:key identifiers are supported.
    Resolving an identifier does not establish that anyone controls its key.
    """
    try:
        key = public_key(did)
    except DidError as exc:
        raise DidResolutionError("invalid or unsupported Ed25519 did:key") from exc
    if not crypto_core_ed25519_is_valid_point(key):
        raise DidResolutionError("invalid or small-order Ed25519 public key")

    fingerprint = did.removeprefix("did:key:")
    method_id = f"{did}#{fingerprint}"
    method = {
        "id": method_id,
        "type": "Multikey",
        "controller": did,
        "publicKeyMultibase": fingerprint,
    }
    return {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/multikey/v1",
        ],
        "id": did,
        "verificationMethod": [method],
        "authentication": [method_id],
        "assertionMethod": [method_id],
        "capabilityInvocation": [method_id],
        "capabilityDelegation": [method_id],
    }
