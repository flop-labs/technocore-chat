"""The stdlib signing backend must remain byte-identical to the native oracle.

Ed25519 signing is deterministic, so checking that both implementations verify
is weaker than comparing their public keys and complete signatures directly.
The server-side ``didkey.verify`` check is retained as the final wire-format
assertion, including the base58 DID and unpadded base64url encoding.
"""

from __future__ import annotations

import base64
import importlib.util
import random
from pathlib import Path

import pytest
from _client import _multibase
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import didkey

ROOT = Path(__file__).resolve().parents[2]
STDLIB_SIGNER = ROOT / "scripts" / "stdlib_ed25519.py"


def load_stdlib_backend():
    spec = importlib.util.spec_from_file_location("stdlib_ed25519_oracle_test", STDLIB_SIGNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def did_for(public: bytes) -> str:
    return f"{didkey.PREFIX}z{_multibase(didkey.MULTICODEC_ED25519 + public)}"


def test_stdlib_backend_matches_cryptography_and_server_verifier() -> None:
    """Compare 200 deterministic cases, then pass each signature through didkey.verify."""
    stdlib = load_stdlib_backend()
    randomizer = random.Random(417)
    cases = [(bytes(32), ""), (bytes([0xFF]) * 32, "all ff")]
    cases.extend(
        (
            bytes(randomizer.randrange(256) for _ in range(32)),
            "".join(randomizer.choice("abc XYZ-世界👋") for _ in range(randomizer.randrange(301))),
        )
        for _ in range(200)
    )

    for seed, text in cases:
        fallback = stdlib.Ed25519PrivateKey.from_private_bytes(seed)
        native = Ed25519PrivateKey.from_private_bytes(seed)
        fallback_public = fallback.public_key().public_bytes_raw()
        native_public = native.public_key().public_bytes_raw()
        fallback_signature = fallback.sign(text.encode("utf-8"))
        native_signature = native.sign(text.encode("utf-8"))

        assert fallback_public == native_public
        assert fallback_signature == native_signature
        didkey.verify(
            did_for(fallback_public),
            base64.urlsafe_b64encode(fallback_signature).decode().rstrip("="),
            text,
        )


def test_stdlib_backend_rejects_non_32_byte_seeds_like_native() -> None:
    stdlib = load_stdlib_backend()
    for seed in (b"", b"x" * 31, b"x" * 33):
        with pytest.raises(ValueError):
            stdlib.Ed25519PrivateKey.from_private_bytes(seed)
        with pytest.raises(ValueError):
            Ed25519PrivateKey.from_private_bytes(seed)
