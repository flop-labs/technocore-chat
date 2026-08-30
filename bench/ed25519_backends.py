"""Why didkey.py verifies with libsodium rather than OpenSSL.

Run: uv run python bench/ed25519_backends.py

Both backends are already project dependencies, so this needs no separate environment.
The signed lane spends ~93% of a write inside `key.verify`, which is what makes this the
one swap worth doing: the base58 decode beside it is ~3%, so a faster base58 cannot
matter no matter how much faster it gets.
"""

from __future__ import annotations

import statistics
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from nacl.signing import VerifyKey

MESSAGE = b"r|standup|status update for today, three items and a blocker"
N = 2000


def per_call_us(verify, n: int = N) -> float:
    """Microseconds per verify, median of 7 batches so one scheduling blip cannot win."""
    verify()
    batches = []
    for _ in range(7):
        start = time.perf_counter()
        for _ in range(n):
            verify()
        batches.append((time.perf_counter() - start) / n * 1e6)
    return statistics.median(batches)


def main() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    signature = private.sign(MESSAGE)

    openssl = Ed25519PublicKey.from_public_bytes(public)
    libsodium = VerifyKey(public)
    # Both must actually verify, or the comparison is between a check and a no-op.
    openssl.verify(signature, MESSAGE)
    libsodium.verify(MESSAGE, signature)

    before = per_call_us(lambda: openssl.verify(signature, MESSAGE))
    after = per_call_us(lambda: libsodium.verify(MESSAGE, signature))
    print(f"cryptography (OpenSSL) {before:8.2f} us/verify")
    print(f"PyNaCl (libsodium)     {after:8.2f} us/verify   {before / after:.2f}x")


if __name__ == "__main__":
    main()
