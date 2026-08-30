"""The wrapper's signing mirror, held in lockstep with the service's own primitives.

`mcp/src/technocore_mcp/signing.py` re-implements three things the wrapper cannot import
from the service: the single-line sweep the signature must cover, the did:key encoding,
and the signature encoding. Each is checked here against the service-side original —
`store.clean_text`, `didkey.public_key`, `didkey.verify` — so drift is a red test in this
repo, not a 403 in someone's deployment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mcp" / "src"))

from technocore_mcp import signing  # noqa: E402

# One seed, every derived value deterministic, so a failure is reproducible.
SEED_HEX = "9d" * 32


def test_the_sweep_matches_the_services_own_for_hostile_input():
    """The signature covers the swept text — exactly the bytes the service stores — so
    the two sweeps must agree on every transformation. The refusal cases (empty after
    sweep, over-long) are deliberately not mirrored: those stay the service's answers."""
    import store

    for text in (
        "plain text",
        "  padded  ",
        "new\nline\r\nand\ttab",
        "zero​width and bidi ‮override",
        "tag characters \U000e0041\U000e0042 smuggled",
        "emoji family 👨‍👩‍👧 flattens",
        "line separator paragraph",
        "unicode текст 中文 🎉 survives",
    ):
        assert signing.sweep(text) == store.clean_text(text), repr(text)


def test_the_did_encoding_is_the_one_the_service_decodes():
    """`didkey.public_key` is the decoder every signed write goes through: the wrapper's
    did:key must round-trip through it back to the very key that will sign."""
    import re

    import didkey

    signer = signing.Signer(bytes.fromhex(SEED_HEX))
    expected = signer._key.public_key().public_bytes_raw()
    assert didkey.public_key(signer.did) == expected
    # …and the published shape holds: the exact pattern /openapi.json advertises.
    assert re.fullmatch(didkey.DID_PATTERN, signer.did)


def test_the_signature_verifies_under_the_services_own_verifier():
    import didkey

    signer = signing.Signer(bytes.fromhex(SEED_HEX))
    canonical = "lobby|17|hello world"
    didkey.verify(signer.did, signer.sign(canonical), canonical)  # raises on any mismatch

    with pytest.raises(didkey.SignatureError):
        didkey.verify(signer.did, signer.sign(canonical), "lobby|17|hello worlD")


def test_the_key_loads_from_both_documented_spellings():
    import base64

    seed = bytes.fromhex(SEED_HEX)
    by_hex = signing.load(SEED_HEX)
    by_b64 = signing.load(base64.urlsafe_b64encode(seed).decode().rstrip("="))
    assert by_hex.did == by_b64.did

    for junk in ("", "abc", "zz" * 32, SEED_HEX + "00"):
        with pytest.raises(ValueError):
            signing.load(junk)


def test_nonces_strictly_increase_even_inside_one_millisecond():
    values = [signing.next_nonce() for _ in range(50)]
    assert values == sorted(set(values))
