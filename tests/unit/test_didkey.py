"""Run: uv run --group dev python -m pytest tests"""

import _client
import pytest
from _client import _keypair, _multibase

client = _client.client  # the shared TestClient fixture


def test_base58decode_preserves_leading_zero_bytes():
    import didkey

    payload = b"\x00\x00" + b"\xab" * 32
    n = int.from_bytes(payload, "big")
    encoded = ""
    while n:
        n, remainder = divmod(n, 58)
        encoded = didkey._B58[remainder] + encoded
    encoded = "11" + encoded

    assert didkey._b58decode(encoded) == payload


def test_base58decode_all_zeroes():
    import didkey

    assert didkey._b58decode("111") == b"\x00\x00\x00"


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

    alias = "XXXXXXXX" + mb
    padded = didkey.PREFIX + "z1" + mb[1:]
    wrong_codec = didkey.PREFIX + "z" + _multibase(b"\xe7\x01" + real)
    assert len(wrong_codec) == len(did), "premise: this must pass the length check to matter"

    for spelling in (alias, padded, wrong_codec):
        with pytest.raises(didkey.DidError):
            didkey.public_key(spelling)
        assert not didkey.is_did(spelling)

    assert didkey.public_key(did) == real
