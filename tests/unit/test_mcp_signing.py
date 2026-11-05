"""The wrapper's signing mirror, held in lockstep with the service's own primitives.

`mcp/src/technocore_mcp/signing.py` re-implements three things the wrapper cannot import
from the service: the single-line sweep the signature must cover, the did:key encoding,
and the signature encoding. Each is checked here against the service-side original —
`store.clean_text`, `didkey.public_key`, `didkey.verify` — so drift is a red test in this
repo, not a 403 in someone's deployment.
"""

from __future__ import annotations

import re
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


def test_the_identity_note_path_matches_the_published_convention():
    """patterns.md §3 and the manual both publish this as prose; `note_path` is its only
    implementation, so the arithmetic is re-derived here rather than restated."""
    import hashlib

    signer = signing.Signer(bytes.fromhex(SEED_HEX))
    namespace, key = signing.note_path(signer.did)

    fingerprint = hashlib.sha256(signer.did.encode()).hexdigest()[:16]
    assert namespace == f"did-{fingerprint[:2]}"
    assert key == fingerprint[2:]
    assert len(key) == 14  # 16 hex characters, less the 2-character shard

    # Both halves have to be writable names, or the note cannot be published at all.
    name = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
    assert name.fullmatch(namespace) and name.fullmatch(key)

    # The unsharded path older readers fall back to is the two halves concatenated.
    assert namespace.removeprefix("did-") + key == fingerprint


def test_nonces_strictly_increase_even_inside_one_millisecond():
    values = [signing.next_nonce() for _ in range(50)]
    assert values == sorted(set(values))


@pytest.mark.parametrize(
    ("body", "floor"),
    [
        # The message lane's refusal, as the service words it (store.append_message).
        (
            "400 nonce 1790439525341 is not greater than 1790328000000000000, the last one "
            "this key used in /r/mb-inbox — a signed URL is single-use, so count up",
            1790328000000000000,
        ),
        # The ownership counter's (app._burn_nonce).
        (
            "403 nonce 1790439525452 was already used for /r/d-desk (last 1790328000000000000)."
            " A signed ownership URL is single-use — count up and sign again.",
            1790328000000000000,
        ),
        # Every other answer, including a caller's own text quoting the wording, is not one.
        ("409 conflict: the note changed", None),
        ("signed writes need a non-negative integer nonce, got 'x' — 1-19 digits", None),
        ("400 duplicate: nonce 5 is not greater than 7 was said here already", None),
        ("", None),
    ],
)
def test_only_a_nonce_refusal_names_a_floor(body, floor):
    """#916: the server re-signs only on the two refusals that name a floor, anchored at
    the start of the body so an echo of a caller's text cannot trigger a re-sign."""
    assert signing.refused_floor(body) == floor


def test_a_raised_floor_bounds_every_later_nonce(monkeypatch):
    monkeypatch.setattr(signing, "_last_nonce", 0)
    signing.raise_floor(1790328000000000000)
    assert signing.next_nonce() == 1790328000000000001
    signing.raise_floor(5)  # a floor only ever rises
    assert signing.next_nonce() == 1790328000000000002
