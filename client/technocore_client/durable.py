"""Making a new file's *directory entry* durable, not just its contents.

`fsync` on a file makes its data durable. It does not make the name durable: on POSIX the
directory entry that points at it lives in the parent directory, and until that is fsynced a
power loss can leave a file whose bytes reached the disk and whose name did not.

That matters least for a cache and most for an identity. `Keyring` writes a seed, fsyncs it,
and returns a usable DID — and without this a host crash immediately afterwards leaves the next
process seeing no seed and generating a *different* key. The advertised persisted identity would
change silently after a successful first run, which is the one failure that cannot be retried.

Raised by @yukkie3276 in review of #803. The nonce store in the same change already fsynced its
parent after `os.replace` and the keyring did not, so this is one rule written once rather than
the same reasoning applied in one place and forgotten in the other.

POSIX, like the rest of this package: fsyncing a directory handle is not available on Windows.
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_dir(directory: Path) -> None:
    """Flush a directory's own entries. Opening it read-only is enough to fsync it."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir_durable(path: Path) -> None:
    """`mkdir -p`, with every directory entry it creates made durable.

    Creating `a/b/c` adds an entry to `a`, to `a/b` and to `a/b/c`'s parent in turn, so syncing
    only the deepest one leaves the shallower entries no more durable than the file would have
    been. The walk stops at the first ancestor that already exists, because its entry was
    somebody else's problem and is not one this call created.
    """
    new: list[Path] = []
    probe = path
    while not probe.exists():
        new.append(probe)
        if probe.parent == probe:  # reached the root; nothing above to sync
            break
        probe = probe.parent

    path.mkdir(parents=True, exist_ok=True)

    # Shallowest first: a parent's entry must be durable before the child's entry inside it
    # means anything.
    for created in reversed(new):
        fsync_dir(created.parent)
