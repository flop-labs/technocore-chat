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


def test_every_nonce_the_pattern_accepts_survives_int_normalisation():
    """A signed record stores int(nonce) and serves it back, so re-verification rebuilds
    `<room>|<nonce>|<text>` from that int. That only works when the string the pattern
    accepted is the one int() reproduces, i.e. str(int(nonce)) == nonce. Pin the invariant
    on the regex itself: "0" stays valid, leading zeros are refused.
    """
    import didkey

    for good in ("0", "1", "7", "10", "9" * 19, str(2**63 - 1)):
        assert didkey.NONCE_RE.fullmatch(good), good
        assert str(int(good)) == good  # the property the write path relies on

    for bad in ("007", "00", "01", "", "9" * 20, " 7", "7 ", "1_000"):
        assert not didkey.NONCE_RE.fullmatch(bad), bad
