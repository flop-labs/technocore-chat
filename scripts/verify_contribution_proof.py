# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography"]
# ///
"""Verify a technocore-contribution-v1 proof without contacting a service.

The proof schema is deliberately small and deterministic.  The signature covers
one compact, sorted JSON object; it does not cover the DID because the DID is the
verification key identified by the proof itself.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "technocore-contribution-v1"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTICODEC_ED25519 = b"\xed\x01"


def canonical_payload(artifact_url: str, commit: str) -> bytes:
    """Return the exact UTF-8 bytes covered by a v1 contribution signature."""
    if not isinstance(artifact_url, str) or not artifact_url:
        raise ValueError("artifact_url must be a non-empty string")
    if not isinstance(commit, str) or not commit or commit != commit.lower():
        raise ValueError("commit must be a non-empty lowercase string")
    return json.dumps(
        {"artifact_url": artifact_url, "commit": commit, "schema": SCHEMA},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _public_key(did: str) -> bytes:
    if not isinstance(did, str) or not did.startswith("did:key:z"):
        raise ValueError("did must be an Ed25519 did:key")
    encoded = did[len("did:key:z") :]
    n = 0
    for char in encoded:
        try:
            n = n * 58 + B58.index(char)
        except ValueError as exc:
            raise ValueError("did contains a non-base58 character") from exc
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    leading = len(encoded) - len(encoded.lstrip("1"))
    raw = b"\x00" * leading + raw
    if len(raw) != 34 or not raw.startswith(MULTICODEC_ED25519):
        raise ValueError("did is not an Ed25519 did:key")
    return raw[2:]


def verify(proof: dict[str, object]) -> None:
    """Raise ValueError unless *proof* is a valid v1 proof."""
    if not isinstance(proof, dict):
        raise ValueError("proof must be a JSON object")
    if set(proof) != {"artifact_url", "commit", "did", "signature"}:
        raise ValueError("proof must contain exactly artifact_url, commit, did, signature")
    did = proof.get("did")
    artifact_url = proof.get("artifact_url")
    commit = proof.get("commit")
    signature = proof.get("signature")
    if (
        not isinstance(did, str)
        or not isinstance(artifact_url, str)
        or not isinstance(commit, str)
        or not isinstance(signature, str)
    ):
        raise ValueError("artifact_url, commit, did and signature are required strings")
    payload = canonical_payload(artifact_url, commit)
    if not signature or "=" in signature:
        raise ValueError("signature must be unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(signature + "===")
    except ValueError as exc:
        raise ValueError("signature is not base64url") from exc
    if len(raw) != 64 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != signature:
        raise ValueError("signature is not canonical unpadded base64url")
    try:
        Ed25519PublicKey.from_public_bytes(_public_key(did)).verify(raw, payload)
    except InvalidSignature as exc:
        raise ValueError("signature does not cover the canonical payload") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proof", nargs="?", default="-", help="proof JSON path, or - for stdin")
    args = parser.parse_args()
    raw = sys.stdin.read() if args.proof == "-" else Path(args.proof).read_text()
    try:
        proof = json.loads(raw)
        verify(proof)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"VERIFIED {proof['did']} {proof['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
