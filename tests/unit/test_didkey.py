"""Run: uv run --group dev python -m pytest tests"""

import _client
import pytest
from _client import _keypair, _multibase

client = _client.client  # the shared TestClient fixture


def test_a_did_key_has_exactly_one_spelling(client):
    """Ownership compares DID *strings*: `_note_write_gate` asks `signer != current`, and
    `_allowed_keys` matches by string. So a key with more than one accepted spelling is a
    key whose owner the service cannot recognise — the caller signs with the same private
    key, presents an alias, and fails its own allow-list.

    Each of the three shapes below decodes to a real key's bytes and is refused only by
    the *other* half of a two-part check. `or` → `and` short-circuits on the common
    operand and silently deletes that half, which is why all three need pinning
    separately rather than as one "malformed DID" case.
    """
    import didkey

    did, _ = _keypair()
    mb = did[len(didkey.PREFIX) :]
    real = didkey.public_key(did)

    # Right suffix, wrong prefix — same length, so only the `startswith` check refuses it.
    alias = "XXXXXXXX" + mb
    # Right prefix and leading `z`, one base58 zero-digit too long. Base58 ignores the
    # padding, so it decodes to the same 34 bytes; only the exact-length check refuses it.
    padded = didkey.PREFIX + "z1" + mb[1:]
    # Right prefix and right length, but the multicodec says something other than
    # ed25519-pub. Only the codec check refuses it.
    wrong_codec = didkey.PREFIX + "z" + _multibase(b"\xe7\x01" + real)
    assert len(wrong_codec) == len(did), "premise: this must pass the length check to matter"

    for spelling in (alias, padded, wrong_codec):
        with pytest.raises(didkey.DidError):
            didkey.public_key(spelling)
        assert not didkey.is_did(spelling)

    assert didkey.public_key(did) == real  # …and the canonical one still works


def test_a_signature_has_exactly_one_spelling(client):
    """The same reasoning as the DID above, one field along. 64 bytes is 512 bits and 86
    base64url characters carry 516, so the last character's low four bits are slack the
    decoder discards: sixteen strings per signature, all decoding to the same bytes.

    Ed25519 is indifferent — it only ever sees the 64 bytes — so this never forged
    anything and the nonce, not the encoding, is what keeps a captured URL single-use.
    What it did break is every consumer that handles the signature as a *string*:
    `SIG_PATTERN` is published in `/openapi.json` as the encoding, a stack that re-encodes
    a signature it decoded gets a different string back, and a record that keeps its
    signature keeps whichever of the sixteen the caller happened to send.
    """
    import base64

    import didkey

    did, sign = _keypair()
    canonical = sign("hello")
    raw = base64.urlsafe_b64decode(canonical + "==")

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    aliases = [
        canonical[:-1] + ch
        for ch in alphabet
        if base64.urlsafe_b64decode(canonical[:-1] + ch + "==") == raw and ch != canonical[-1]
    ]
    assert len(aliases) == 15, "premise: base64 leaves four slack bits, so sixteen spellings"

    for alias in aliases:
        # Refused on the encoding, before verification — the bytes it decodes to are the
        # bytes of a signature that does verify, so a SignatureError here would be wrong.
        with pytest.raises(didkey.DidError):
            didkey.verify(did, alias, "hello")

    didkey.verify(did, canonical, "hello")  # …and the canonical one still verifies


def test_the_signed_lane_refuses_an_aliased_signature_over_http(client):
    """Externally observable: a 400 on the encoding, and nothing lands in the room."""
    import base64

    did, sign = _keypair()
    canonical = sign("alias|1|hi")
    raw = base64.urlsafe_b64decode(canonical + "==")
    # Same 64 bytes, any last character but the one the canonical encoder produced.
    alias = next(
        canonical[:-1] + ch
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        if ch != canonical[-1] and base64.urlsafe_b64decode(canonical[:-1] + ch + "==") == raw
    )

    refused = client.get(f"/r/alias/say-signed/{did}/{alias}/1/hi")
    assert refused.status_code == 400
    assert client.get("/r/alias?format=json").json()["messages"] == []

    assert _client._say_signed(client, "alias", did, sign, "hi").status_code == 200


def test_b58_leading_zero_bytes_round_trip():
    """base58btc encodes leading 0x00 bytes as leading '1' characters.

    Without this, a payload whose raw bytes start with 0x00 loses those bytes
    during int→bytes conversion and the decoder returns fewer bytes than the
    encoder put in.  This is not reachable from a *real* Ed25519 did:key (the
    multicodec prefix 0xed01 never starts with 0x00), but the codec is a
    general-purpose primitive and the spec requires the round-trip to hold for
    all inputs.
    """
    import didkey

    # A payload with two leading zero bytes
    payload = b"\x00\x00" + b"\xab" * 32
    encoded = _multibase(payload)
    # The encoder must emit one '1' per leading 0x00 byte — if it doesn't,
    # the decoder has nothing to recover and the round-trip silently shrinks.
    assert encoded.startswith("11"), (
        f"encoder must emit leading '1's for 0x00 bytes, got {encoded[:4]!r}"
    )
    decoded = didkey._b58decode(encoded)
    assert decoded == payload, (
        f"leading zeros lost: encoded {len(payload)}B, decoded {len(decoded)}B"
    )


def test_abbreviate_does_not_collide_two_honest_verified_signers():
    """Issue #300: at 4 trailing characters, two honestly-generated keys collided in
    production — 1,452 real collision pairs observed. This test uses the exact victim/forged
    pair from the issue: they share the trailing 4 characters 'QAtx' but differ well before
    that. At 8 characters they render distinctly.

    The victim key is the issue author's real contributor identity from #177/#178.
    The forged key was ground to match its 4-char marker in 175 seconds of search.
    """
    import didkey

    victim = "did:key:z6MkmDkcrgAGa2DZ9qxfmMjNpwaKBXkDt3owfUPKyUxRQAtx"
    forged = "did:key:z6MkhT9hrBzwZMLiYY22v9wEKyUDrgFWogmdZni9Z1EhQAtx"

    # Premise: they are distinct keys
    assert didkey.public_key(victim) != didkey.public_key(forged)

    # At 4 trailing chars, they collided (this would pass on the unfixed code)
    victim_last_4 = victim[-4:]
    forged_last_4 = forged[-4:]
    assert victim_last_4 == forged_last_4 == "QAtx", "premise: they share the last 4 chars"

    # After the fix, the 8-char abbreviation must distinguish them
    victim_abbrev = didkey.abbreviate(victim)
    forged_abbrev = didkey.abbreviate(forged)
    assert victim_abbrev != forged_abbrev, (
        f"abbreviate() still collides on honest keys: {victim_abbrev!r} == {forged_abbrev!r}"
    )


def test_abbreviate_shows_eight_trailing_characters():
    """Pin the marker width at 8 trailing characters so it cannot silently narrow again.

    The constant 'z6Mk' prefix contributes no identity (every Ed25519 did:key starts with it),
    so the marker's discriminating content is entirely in the trailing characters. At 4 chars
    that was 23.4 bits; at 8 chars it is 46.9 bits, pushing the birthday collision from ~4k
    identities out to ~10M.
    """
    import didkey

    did, _ = _keypair()
    abbrev = didkey.abbreviate(did)

    # The marker must start with the fixed prefix
    assert abbrev.startswith("z6Mk…"), f"marker must start 'z6Mk…', got {abbrev!r}"

    # The suffix must be exactly 8 characters
    suffix = abbrev.split("…")[-1]
    assert len(suffix) == 8, f"marker must show 8 trailing chars, got {len(suffix)}: {abbrev!r}"

    # The suffix must match the last 8 characters of the did:key's multibase encoding
    mb = did[len(didkey.PREFIX):]
    assert suffix == mb[-8:], f"suffix must be the last 8 chars of the multibase: expected {mb[-8:]!r}, got {suffix!r}"
