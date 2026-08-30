"""Run: uv run --group dev python -m pytest tests

0.9.0 moved signature verification from OpenSSL to libsodium. The two are not obliged to
agree: Ed25519 implementations are known to differ on edge-case signatures — small-order
public keys, non-canonical scalars — and "RFC 8032 conformant" does not settle those. This
service treats a verdict as a gate (mailboxes and owned rooms refuse writes without it), so
a backend swap that quietly moved the accept/reject boundary would be a security change
wearing a performance change's clothes.

`cryptography` is still a dependency, which makes it available as an oracle: every case
below asks both libraries and asserts they returned the same verdict, rather than asserting
libsodium matches a table of expectations written by hand. Deterministic seed so a failure
is reproducible.
"""

import random

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

# The order of the Ed25519 group. A signature whose S is reduced mod L is canonical; S + L
# encodes the same scalar and is the classic malleability probe.
GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

# Public keys of order 1, 2, 4 or 8. The subgroup that separates "cofactored" verification
# from "cofactorless", and where implementations most often part company.
SMALL_ORDER_KEYS = [
    bytes.fromhex(h)
    for h in (
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0100000000000000000000000000000000000000000000000000000000000000",
        "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "e0eb7a7c3b41b8ae1656e3faf19fc46ada098deb9c32b1fd866205165f49b800",
        "5f9c95bca3508c24b1d0b1559c83ef5b04445cc4581c8e86d8224eddd09f1157",
    )
]


def openssl_accepts(public: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def libsodium_accepts(public: bytes, signature: bytes, message: bytes) -> bool:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    try:
        VerifyKey(public).verify(message, signature)
    except (BadSignatureError, ValueError, TypeError):
        return False
    return True


def both(public: bytes, signature: bytes, message: bytes) -> tuple[bool, bool]:
    return openssl_accepts(public, signature, message), libsodium_accepts(
        public, signature, message
    )


def test_the_two_backends_agree_on_good_and_tampered_signatures() -> None:
    """The bulk case: 400 keypairs, each checked valid, with a flipped signature bit, with a
    flipped message bit, and against someone else's key. Both libraries must return the same
    verdict every time — and the valid case must actually be accepted, or this would pass
    just as well against two functions that reject everything."""
    rnd = random.Random(1234)
    accepted = 0
    for _ in range(400):
        private = Ed25519PrivateKey.from_private_bytes(bytes(rnd.randrange(256) for _ in range(32)))
        public = private.public_key().public_bytes_raw()
        message = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 80)))
        signature = private.sign(message)

        openssl, libsodium = both(public, signature, message)
        assert openssl and libsodium, "a freshly made signature must verify under both"
        accepted += 1

        flipped = bytearray(signature)
        flipped[rnd.randrange(64)] ^= 1 << rnd.randrange(8)
        assert both(public, bytes(flipped), message) == (False, False)

        if message:
            altered = bytearray(message)
            altered[rnd.randrange(len(altered))] ^= 1
            assert both(public, signature, bytes(altered)) == (False, False)

        stranger = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        assert both(stranger, signature, message) == (False, False)
    assert accepted == 400


@pytest.mark.parametrize("public", SMALL_ORDER_KEYS)
def test_the_two_backends_agree_on_small_order_public_keys(public: bytes) -> None:
    """The documented divergence area, pinned rather than assumed. If a future release of
    either library starts accepting one of these, this fails and the swap gets re-examined
    instead of silently changing who may write to a mailbox."""
    rnd = random.Random(99)
    for _ in range(4):
        signature = bytes(rnd.randrange(256) for _ in range(64))
        openssl, libsodium = both(public, signature, b"x")
        assert openssl == libsodium, f"backends disagree on a small-order key: {public.hex()}"


def test_the_two_backends_agree_on_a_non_canonical_scalar() -> None:
    """Signature malleability: S + L encodes the same scalar as S. A verifier that does not
    reduce accepts a second, different byte string for one signed message — which for this
    service would mean one message re-appearing under a fresh nonce."""
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = private.public_key().public_bytes_raw()
    message = b"r|standup|status"
    signature = private.sign(message)

    assert both(public, signature, message) == (True, True)
    scalar = int.from_bytes(signature[32:], "little")
    mutated = signature[:32] + ((scalar + GROUP_ORDER) % 2**256).to_bytes(32, "little")
    assert both(public, mutated, message) == (False, False), "a malleable signature was accepted"


def test_didkey_verify_matches_the_openssl_oracle() -> None:
    """…and the same agreement holds through the real entry point, not just the raw
    libraries. `didkey.verify` adds the did:key parse, the base64url decode and the
    DidError/SignatureError split on top of the backend, and it is what app.py calls."""
    import base64

    from _client import _multibase

    import didkey

    rnd = random.Random(7)
    for _ in range(60):
        private = Ed25519PrivateKey.from_private_bytes(bytes(rnd.randrange(256) for _ in range(32)))
        public = private.public_key().public_bytes_raw()
        did = didkey.PREFIX + "z" + _multibase(b"\xed\x01" + public)
        message = f"r|room|message {rnd.randrange(10_000)}"
        signature = private.sign(message.encode())
        if rnd.random() < 0.5:  # half the cases are corrupted, so both verdicts are exercised
            corrupt = bytearray(signature)
            corrupt[rnd.randrange(64)] ^= 1 << rnd.randrange(8)
            signature = bytes(corrupt)

        encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        try:
            didkey.verify(did, encoded, message)
            verified = True
        except didkey.SignatureError:
            verified = False
        assert verified == openssl_accepts(public, signature, message.encode())
