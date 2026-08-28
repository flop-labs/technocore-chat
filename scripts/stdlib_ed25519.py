"""Small, dependency-free Ed25519 signing backend for ``scripts/sign.py``.

The command-line signer prefers ``cryptography`` when that import works. This
module is the portability path for a fixed Python runtime where packages cannot
be installed. It implements the signing half of RFC 8032; verification remains
on the server's PyNaCl-backed path, and native cryptography stays preferred for
performance when it is available. The fallback is portability-oriented pure
Python rather than a constant-time implementation.
"""

from __future__ import annotations

import hashlib
from typing import Final

_P: Final = 2**255 - 19
_L: Final = 2**252 + 27742317777372353535851937790883648493
_D: Final = (-121665 * pow(121666, _P - 2, _P)) % _P

# The Edwards25519 base point, in extended coordinates (X, Y, Z, T).
_BASE_X: Final = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BASE_Y: Final = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_BASE: Final = (_BASE_X, _BASE_Y, 1, (_BASE_X * _BASE_Y) % _P)
_IDENTITY: Final = (0, 1, 1, 0)


def _point_add(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Add two Edwards25519 points in extended coordinates."""
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * _D * t1 * t2) % _P
    d = (2 * z1 * z2) % _P
    e = (b - a) % _P
    f = (d - c) % _P
    g = (d + c) % _P
    h = (b + a) % _P
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalar_mult(
    scalar: int, point: tuple[int, int, int, int] = _BASE
) -> tuple[int, int, int, int]:
    """Multiply a point by a non-negative integer."""
    result = _IDENTITY
    while scalar:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _encode(point: tuple[int, int, int, int]) -> bytes:
    """Encode an extended-coordinate point as an Edwards25519 public key."""
    x, y, z, _ = point
    inverse = pow(z, _P - 2, _P)
    x = (x * inverse) % _P
    y = (y * inverse) % _P
    encoded = bytearray(y.to_bytes(32, "little"))
    encoded[31] |= (x & 1) << 7
    return bytes(encoded)


def _expanded(seed: bytes) -> tuple[int, bytes]:
    """Return the clamped secret scalar and the signing prefix."""
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


class Ed25519PublicKey:
    """The small public-key surface used by ``scripts/sign.py``."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def public_bytes_raw(self) -> bytes:
        return self._raw


class Ed25519PrivateKey:
    """A seed-backed Ed25519 private key with the cryptography API's two methods."""

    __slots__ = ("_seed", "_scalar", "_prefix", "_public")

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise ValueError("Ed25519 private keys must be 32 bytes")
        self._seed = bytes(seed)
        self._scalar, self._prefix = _expanded(self._seed)
        self._public = _encode(_scalar_mult(self._scalar))

    @classmethod
    def from_private_bytes(cls, data: bytes) -> Ed25519PrivateKey:
        return cls(data)

    def public_key(self) -> Ed25519PublicKey:
        return Ed25519PublicKey(self._public)

    def sign(self, data: bytes) -> bytes:
        message = bytes(data)
        nonce = int.from_bytes(hashlib.sha512(self._prefix + message).digest(), "little") % _L
        encoded_nonce = _encode(_scalar_mult(nonce))
        challenge = (
            int.from_bytes(
                hashlib.sha512(encoded_nonce + self._public + message).digest(), "little"
            )
            % _L
        )
        response = (nonce + challenge * self._scalar) % _L
        return encoded_nonce + response.to_bytes(32, "little")
