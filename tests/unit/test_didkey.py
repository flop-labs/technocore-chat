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


def test_abbreviate_does_not_collide_two_honest_verified_signers():
    """#300: `abbreviate()` used to show only the 4 trailing base58 characters (~23.4 bits)
    of a `did:key`, on top of the 4 leading characters that are *always* `z6Mk` — the fixed
    `ed25519-pub` multicodec tag, constant across every Ed25519 key and so discriminating
    nothing. Two different, honestly-generated verified signers below share those trailing 4
    characters (`QAtx`) and so rendered as the identical `<z6Mk…QAtx>` marker — a real,
    reported collision (github.com/flop-labs/technocore-chat/issues/300), not a
    birthday-paradox estimate. They differ well before the last 4 characters, so widening the
    shown suffix (now 8 trailing characters, ~46.9 bits) tells them apart.
    """
    import didkey

    victim = "did:key:z6MkmDkcrgAGa2DZ9qxfmMjNpwaKBXkDt3owfUPKyUxRQAtx"
    forged = "did:key:z6MkhT9hrBzwZMLiYY22v9wEKyUDrgFWogmdZni9Z1EhQAtx"

    assert didkey.is_did(victim) and didkey.is_did(forged)
    assert didkey.public_key(victim) != didkey.public_key(forged)  # distinct keys
    assert victim[-4:] == forged[-4:] == "QAtx"  # both collided under the old 4-char marker

    assert didkey.abbreviate(victim) != didkey.abbreviate(forged)


def test_abbreviate_shows_eight_trailing_characters():
    """The leading `z6Mk` is constant for every Ed25519 did:key, so it carries no identity —
    the marker's discriminating budget is the trailing run. Pin it at 8 characters (not 4)
    so a future edit cannot silently narrow the window back down without failing here.
    """
    from _client import _keypair

    import didkey

    did, _ = _keypair(seed=7)
    marker = didkey.abbreviate(did)
    prefix, _, suffix = marker.partition("…")

    assert prefix == "z6Mk"
    assert suffix == did[len(didkey.PREFIX) :][-8:]
    assert len(suffix) == 8
