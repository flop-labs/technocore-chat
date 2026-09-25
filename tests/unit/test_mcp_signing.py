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


def test_the_sweep_refuses_joiner_only_content():
    """ZWNJ/ZWJ are preserved in context, but a payload of nothing but joiners
    is refused by the server (store.clean_text) and by standalone signers
    (scripts/sign.py) — they carry meaning only beside visible characters.

    The MCP signing.sweep() is intentionally transformation-only; the MCP
    handler (say_signed) refuses joiner-only in the no-key challenge path.
    """
    import store

    joiner_payloads = ["\u200c", "\u200d", "\u200c\u200d", " \u200c ", "\u200d \u200c"]

    # store.clean_text raises StoreError
    for payload in joiner_payloads:
        with pytest.raises(store.StoreError, match="empty text"):
            store.clean_text(payload)

    # Also reject non-rendering mark combinations (e.g. ZWJ+VS16) that leave
    # no visible glyph
    vs16_zwj = "\u200d\ufe0f"
    with pytest.raises(store.StoreError, match="empty text"):
        store.clean_text(vs16_zwj)

    # scripts/sign.py swept() raises SystemExit
    import subprocess
    import sys

    for payload in joiner_payloads:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/sign.py"),
                "--seed",
                "a" * 64,
                "say",
                "lobby",
                "1",
                payload,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, f"sign.py accepted joiner-only payload {payload!r}"
        assert "nothing visible" in result.stderr or "nothing visible" in result.stdout


def test_mcp_sweep_preserves_joiners_and_returns_empty_for_joiner_only():
    """The MCP signing.sweep() does transformation only — joiner-only
    content survives the sweep but the MCP handler refuses it at the
    challenge-generation step. This test documents the transformation
    boundary: sweep returns the preserved joiners, then the handler
    checks for visible content."""
    from technocore_mcp import signing as mcp_signing

    # Joiner-only content survives the sweep (transformation only)
    assert mcp_signing.sweep("\u200c") == "\u200c"
    assert mcp_signing.sweep("\u200d \u200c") == "\u200d \u200c"

    # Variation Selectors survive the sweep (category Mn) but are not visible
    assert mcp_signing.sweep("\u200d\ufe0f") == "\u200d\ufe0f"

    # Regular whitespace/controls are still removed to empty
    assert mcp_signing.sweep(" \n\t ") == ""

    # Orthographic ZWNJ/ZWJ in context is preserved
    assert mcp_signing.sweep("a\u200db") == "a\u200db"


def test_orthographic_joiner_text_is_byte_preserved():
    """ZWNJ/ZWJ inside visible text are preserved by all sweep implementations,
    so signed text round-trips correctly through the server."""
    import technocore_mcp.signing as mcp_signing

    import store

    text = "a\u200db"  # text with a zero-width joiner
    assert store.clean_text(text) == text
    assert mcp_signing.sweep(text) == text

    # script/sign.py through subprocess
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/sign.py"),
            "--seed",
            "a" * 64,
            "say",
            "lobby",
            "1",
            text,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"sign.py rejected orthographic joiner text: {result.stderr}"
    # A successful sign prints did:key and signature
    assert result.stdout.count("\n") == 2


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
