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
import binascii
import os
import stat
import tempfile
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
        """The seed on disk, minting one if there is none.

        Every path out of here fsyncs the directory before returning, not only the one that
        created the file. A constructor that returns an identity is promising that identity
        survives a power cut, and the promise cannot depend on which branch it took: a process
        whose `exists()` check lands after another's `os.link` but before that other's fsync
        would otherwise return a working DID whose name is not yet durable (@Minh3132, #803).

        The first fix covered only the lost-create race, which left the plain already-there path
        open — and the test caught that, because it counted directory syncs globally and was
        acquitted by the winner's. Attributing each sync to the thread that made it is what
        turned a passing test into a failing one. One fsync per process on an already-synced
        directory is cheap; reasoning about which interleavings need it is not.
        """
        try:
            return self._load_inner()
        finally:
            if self._path.parent.exists():
                fsync_dir(self._path.parent)

    def _load_inner(self) -> bytes:
        if not self._path.exists():
            created = self._create()
            if created is not None:
                return created
            # Another process created the seed between our check and our create. Its file is
            # complete by construction — see `_create` — so there is nothing to wait for; fall
            # through and read the winner's identity rather than failing a startup that has no
            # reason to fail (@yukkie3276, #803).
            #
            # But fsync the directory before doing so. The winner links the name and fsyncs the
            # directory afterwards, so a loser that read the file and returned in between would
            # hand back a working identity whose *name* was not yet durable — the same power-loss
            # window the directory fsync exists to close, reopened for whichever process did not
            # create the file (@Minh3132, #803). fsync on an already-synced directory is cheap and
            # this runs once per process, so ordering it here rather than reasoning about the
            # interleaving is the trade worth making.
        mode = stat.S_IMODE(self._path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{self._path} is mode {mode:04o}; a signing seed must not be group- or "
                "world-readable. Fix with chmod 600 and rotate if it was ever shared."
            )
        # `validate=True`, and this is not a detail. Without it `urlsafe_b64decode` *silently
        # discards* characters outside the alphabet: "not base64 at all!!" decodes to ten bytes
        # rather than raising. A corrupted seed therefore does not fail — it becomes a different
        # 32-byte value whenever the garbage happens to be the right length, and the identity
        # quietly changes. The length check below is not a substitute; it catches most
        # corruptions and not the one that matters.
        #
        # Raised as a labelled refusal for the same reason the nonce ledger's is: an unreadable
        # identity is unknown state, and a bare `binascii.Error` traceback tells the operator
        # neither what it cost nor what to do (found in review by @yukkie3276, #803).
        try:
            # `b64decode` with `altchars`, because `urlsafe_b64decode` takes no `validate`
            # argument — the urlsafe wrapper is the lenient one, and leniency is the bug here.
            body = self._path.read_text().strip()
            # Pad to a multiple of four rather than always appending "==": with validate=True,
            # surplus padding is itself an error ("Excess data after padding"), so the sloppy
            # version that worked under the lenient decoder does not survive the strict one.
            seed = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                f"{self._path} is not base64 ({exc}). A seed that cannot be read is not a seed "
                "that is absent: generating a new one here would silently change this identity, "
                "and every signature and published note already names the old key. Repair the "
                "file or move it aside deliberately."
            ) from exc
        if len(seed) != 32:
            raise ValueError(
                f"{self._path} decodes to {len(seed)} bytes, not 32. Same reasoning as above: "
                "this is a damaged identity, not a missing one."
            )
        return seed

    def _create(self) -> bytes | None:
        """Mint and persist a seed, or return None if another process got there first.

        Written to a temporary from `mkstemp` and then `os.link`ed into place, rather than
        opened at the final path and filled in afterwards. Two reasons, and the second is the one that
        removes a whole class of retry logic:

        * `link` is create-or-fail — it raises `FileExistsError` rather than clobbering — so it
          is the atomic claim on the name that `O_EXCL` was doing before.
        * the name never exists holding a partial seed. A process that loses the race sees
          either no file or a complete one, so it can read the winner's identity immediately
          instead of waiting for bytes that may still be arriving.

        The temporary is created 0600 by `mkstemp` rather than written and then chmod'ed:
        between those two calls the seed would exist at the process umask, and that window is
        the whole exposure. The link preserves the mode, so the final file is 0600 without a
        second syscall. `mkstemp` also makes the staging name unique per *attempt* rather than
        per process, which a pid was not: two threads share a pid.
        """
        seed = Ed25519PrivateKey.generate().private_bytes_raw()
        mkdir_durable(self._path.parent)
        # `mkstemp`, not a pid-derived name. A pid is unique per process and two *threads* in one
        # process share it, so both would build the same staging path and the loser would raise
        # at the temporary's own O_EXCL — before ever reaching the link that handles the race
        # (@yukkie3276, #803). mkstemp is the stdlib primitive for exactly this: a name nobody
        # else has, created O_CREAT|O_EXCL at 0600, which is the mode this file needs anyway.
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f"{self._path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(base64.urlsafe_b64encode(seed).decode().rstrip("="))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, self._path)
            except FileExistsError:
                return None
        finally:
            tmp.unlink(missing_ok=True)
        # The bytes are durable; the *name* is not until the directory holding it is synced.
        # Without this the constructor can return a working identity and a power loss can leave
        # the next process generating a different one (@yukkie3276, #803).
        fsync_dir(self._path.parent)
        return seed

    def sign(self, message: str) -> str:
        """Sign a canonical string, returning the 86-character base64url the server takes."""
        signature = Ed25519PrivateKey.from_private_bytes(self.seed).sign(message.encode())
        return base64.urlsafe_b64encode(signature).decode().rstrip("=")
