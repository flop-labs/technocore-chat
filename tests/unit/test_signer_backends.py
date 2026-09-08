"""The stdlib signing backend must remain byte-identical to the native oracle.

Ed25519 signing is deterministic, so checking that both implementations verify
is weaker than comparing their public keys and complete signatures directly.
The server-side ``didkey.verify`` check is retained as the final wire-format
assertion, including the base58 DID and unpadded base64url encoding.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import random
import sys
import types
from pathlib import Path

import pytest
from _client import _multibase
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import didkey

ROOT = Path(__file__).resolve().parents[2]
STDLIB_SIGNER = ROOT / "scripts" / "stdlib_ed25519.py"
SIGNER_PATH = ROOT / "scripts" / "sign.py"

RFC_8032_VECTORS = (
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        b"",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        bytes.fromhex("72"),
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
)


def load_stdlib_backend():
    spec = importlib.util.spec_from_file_location("stdlib_ed25519_oracle_test", STDLIB_SIGNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def isolated_cryptography(block: bool = True):
    """Isolate sys.modules so imports of cryptography can be blocked or simulated."""
    saved_modules = dict(sys.modules)
    try:
        for k in list(sys.modules.keys()):
            if k == "cryptography" or k.startswith("cryptography."):
                del sys.modules[k]
        if block:
            sys.modules["cryptography"] = None  # ty: ignore[invalid-assignment]
        yield
    finally:
        for k in list(sys.modules.keys()):
            if k not in saved_modules:
                del sys.modules[k]
        sys.modules.update(saved_modules)


def load_signer(module_name: str = "signer_module"):
    spec = importlib.util.spec_from_file_location(module_name, SIGNER_PATH)
    assert spec is not None and spec.loader is not None
    signer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signer)
    return signer


def did_for(public: bytes) -> str:
    return f"{didkey.PREFIX}z{_multibase(didkey.MULTICODEC_ED25519 + public)}"


def test_stdlib_backend_matches_rfc8032_vectors() -> None:
    """The stdlib backend reproduces official RFC 8032 test vectors directly."""
    stdlib = load_stdlib_backend()
    for seed, message, public, signature in RFC_8032_VECTORS:
        key = stdlib.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
        assert key.public_key().public_bytes_raw().hex() == public
        assert key.sign(message).hex() == signature


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


def test_signer_import_fallback_when_cryptography_unavailable() -> None:
    """The signer falls back to stdlib Ed25519 when cryptography cannot be imported.

    Exercises the full import-fallback path and proves RFC 8032 vector compliance
    through sign.py's key loading, public key derivation, DID generation, and signing.
    """
    with isolated_cryptography(block=True):
        signer = load_signer("signer_no_cryptography")
        assert signer._CryptoPrivateKey is None
        assert signer.Ed25519PublicKey is None
        assert issubclass(signer.InvalidSignature, Exception)

        for seed, message, public, signature in RFC_8032_VECTORS:
            key, _ = signer.load_key(seed)
            assert isinstance(key, signer._StdlibPrivateKey)
            pub_raw = key.public_key().public_bytes_raw()
            assert pub_raw.hex() == public
            sig_raw = key.sign(message)
            assert sig_raw.hex() == signature

            did = signer.did_of(key)
            assert did.startswith("did:key:z6Mk")

            # Wire format sign & verify:
            message_str = message.decode("latin1")
            sig_b64 = signer.signature(key, message_str)
            didkey.verify(did, sig_b64, message_str)

            # Fallback public key resolution:
            pub_key = signer.public_key(did)
            assert isinstance(pub_key, signer._FallbackPublicKey)
            assert pub_key.public_bytes_raw() == pub_raw

        # Refuse delegation verification cleanly without crashing:
        bad_note = f"delegate: {did} * 9999999999 1 fake_sig"
        with pytest.raises(SystemExit) as exc:
            signer.check_note(did, bad_note)
        assert "delegation verification requires cryptography" in str(exc.value)


def test_signer_import_fallback_when_cryptography_raises_base_exception() -> None:
    """Import fallback handles broken pyo3 wheels raising BaseException outside Exception."""

    class _PanicException(BaseException):
        pass

    class _BrokenCrypto(types.ModuleType):
        def __getattr__(self, name: str) -> None:
            raise _PanicException("simulated pyo3 panic on import")

    with isolated_cryptography(block=False):
        sys.modules["cryptography"] = _BrokenCrypto("cryptography")
        signer = load_signer("signer_broken_import")
        assert signer._CryptoPrivateKey is None
        assert signer.Ed25519PublicKey is None

        key, _ = signer.load_key(RFC_8032_VECTORS[0][0])
        assert isinstance(key, signer._StdlibPrivateKey)
        assert key.public_key().public_bytes_raw().hex() == RFC_8032_VECTORS[0][2]
        assert key.sign(RFC_8032_VECTORS[0][1]).hex() == RFC_8032_VECTORS[0][3]


def test_signer_construction_fallback_when_methods_raise_base_exception() -> None:
    """Construction fallback handles pyo3 panics in from_private_bytes/from_public_bytes."""
    signer = load_signer("signer_construction_fallback")

    class _PanicException(BaseException):
        pass

    class BrokenPrivateKey:
        @classmethod
        def from_private_bytes(cls, data: bytes):
            raise _PanicException("simulated pyo3 panic in from_private_bytes")

    class BrokenPublicKey:
        @classmethod
        def from_public_bytes(cls, data: bytes):
            raise _PanicException("simulated pyo3 panic in from_public_bytes")

    signer._CryptoPrivateKey = BrokenPrivateKey
    signer.Ed25519PublicKey = BrokenPublicKey

    for seed, message, public, signature in RFC_8032_VECTORS:
        key = signer._new_key(bytes.fromhex(seed))
        assert isinstance(key, signer._StdlibPrivateKey)
        assert key.public_key().public_bytes_raw().hex() == public
        assert key.sign(message).hex() == signature

        did = signer.did_of(key)
        pub_key = signer.public_key(did)
        assert isinstance(pub_key, signer._FallbackPublicKey)
        assert pub_key.public_bytes_raw().hex() == public

    # Check note refuses delegation cleanly:
    bad_note = f"delegate: {did} * 9999999999 1 fake_sig"
    with pytest.raises(SystemExit) as exc:
        signer.check_note(did, bad_note)
    assert "delegation verification requires cryptography" in str(exc.value)

    # Control-flow exceptions must not be caught:
    class InterruptPrivateKey:
        @classmethod
        def from_private_bytes(cls, data: bytes):
            raise KeyboardInterrupt()

    signer._CryptoPrivateKey = InterruptPrivateKey
    with pytest.raises(KeyboardInterrupt):
        signer._new_key(bytes.fromhex(RFC_8032_VECTORS[0][0]))

    class ExitPublicKey:
        @classmethod
        def from_public_bytes(cls, data: bytes):
            raise SystemExit(7)

    signer.Ed25519PublicKey = ExitPublicKey
    with pytest.raises(SystemExit) as exc:
        signer.public_key(did)
    assert exc.value.code == 7


def test_fallback_public_key_and_loader_in_signer() -> None:
    """Verify _FallbackPublicKey behavior and sign.py's sibling stdlib import loader."""
    signer = load_signer("signer_module_direct")

    # _FallbackPublicKey preserves public bytes and refuses verify cleanly:
    raw_key = b"A" * 32
    fallback_pk = signer._FallbackPublicKey(raw_key)
    assert fallback_pk.public_bytes_raw() == raw_key
    with pytest.raises(SystemExit) as exc:
        fallback_pk.verify(b"sig", b"data")
    assert "delegation verification requires cryptography" in str(exc.value)

    # InvalidSignature shim inherits from Exception:
    assert issubclass(signer.InvalidSignature, Exception)


def test_signer_raw_public_supports_pre40_cryptography() -> None:
    """Pre-40 cryptography has public_bytes(Encoding.Raw, PublicFormat.Raw) but no public_bytes_raw."""
    signer = load_signer("signer_pre40_test")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(RFC_8032_VECTORS[0][0]))
    pub = key.public_key()

    class Pre40PubStub:
        def public_bytes(self, encoding: object, fmt: object) -> bytes:
            return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)

    stub = Pre40PubStub()
    assert not hasattr(stub, "public_bytes_raw")
    raw = signer._raw_public(stub)
    assert raw.hex() == RFC_8032_VECTORS[0][2]

    class MockKey:
        def public_key(self) -> Pre40PubStub:
            return stub

    assert signer.did_of(MockKey()) == did_for(bytes.fromhex(RFC_8032_VECTORS[0][2]))
