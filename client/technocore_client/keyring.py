"""Ed25519 key generation and local persistence, with the DID derived rather than stored.

Storing the `did:key` beside the seed would let the two disagree — a truncated write, a
hand-edited file — and a client that trusts a stored DID signs under an identity it does
not hold. The DID is a pure function of the seed, so it is computed on load and the file
holds one thing.

Permissions are 0600 and are checked on read, not only set on write: a seed that became
world-readable between runs is the failure this catches, and it is silent otherwise.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import didkey

from .durable import fsync_dir, mkdir_durable

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    """base58btc, the inverse of `didkey._b58decode`.

    Leading zero bytes are not carried by the integer and each one is a significant '1'
    in base58btc. The multicodec prefix (0xed) means that never fires for a did:key; it
    is handled anyway so this is the inverse rather than nearly the inverse.
    """
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, digit = divmod(number, 58)
        out = _B58[digit] + out
    return "1" * pad + out


def did_from_seed(seed: bytes) -> str:
    """The `did:key:z6Mk…` for a 32-byte Ed25519 seed."""
    if len(seed) != 32:
        raise ValueError(f"seed must be 32 bytes, got {len(seed)}")
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    return didkey.PREFIX + "z" + _b58encode(didkey.MULTICODEC_ED25519 + public)


class Keyring:
    """One seed on disk, its DID derived on load."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.seed = self._load()
        self.did = did_from_seed(self.seed)

    def _load(self) -> bytes:
        if not self._path.exists():
            return self._create()
        mode = stat.S_IMODE(self._path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{self._path} is mode {mode:04o}; a signing seed must not be group- or "
                "world-readable. Fix with chmod 600 and rotate if it was ever shared."
            )
        seed = base64.urlsafe_b64decode(self._path.read_text().strip() + "==")
        if len(seed) != 32:
            raise ValueError(f"{self._path} does not hold a 32-byte seed")
        return seed

    def _create(self) -> bytes:
        seed = Ed25519PrivateKey.generate().private_bytes_raw()
        mkdir_durable(self._path.parent)
        # Opened 0600 rather than written and then chmod'ed: between those two calls the
        # seed exists at the process umask, and that window is the whole exposure.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(base64.urlsafe_b64encode(seed).decode().rstrip("="))
            handle.flush()
            os.fsync(handle.fileno())
        # The bytes are durable; the *name* is not until the directory holding it is synced.
        # Without this the constructor can return a working identity and a power loss can leave
        # the next process generating a different one (@yukkie3276, #803).
        fsync_dir(self._path.parent)
        return seed

    def sign(self, message: str) -> str:
        """Sign a canonical string, returning the 86-character base64url the server takes."""
        signature = Ed25519PrivateKey.from_private_bytes(self.seed).sign(message.encode())
        return base64.urlsafe_b64encode(signature).decode().rstrip("=")
