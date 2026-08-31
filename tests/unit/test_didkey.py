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
