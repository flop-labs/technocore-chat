"""Crash-safe nonce allocation for the signed lane.

The server's rule is that a nonce must be strictly greater than the last one *that key*
used *in that room* — per (key, room), never global. A client that allocates badly does
not fail loudly: it gets a 4xx naming a number it thought it had never used, and the
cause is a file it wrote three restarts ago.

Two properties, and the second is the whole reason this file exists rather than a counter
in memory:

**Monotonic across concurrent processes, not merely across restarts.** The first version of
this claimed the former and delivered the latter: the wall clock plus a process-local
high-water mark keeps a *restart* safe, and does nothing for two processes that load the same
persisted value in the same millisecond — they compute the same number and one signed write is
refused. Raised by @yukkie3276 in review, and correct. Reload, allocate and flush now happen
under an exclusive `flock` on a sidecar lock file, so the read-modify-write is serialized
across processes and each one sees the previous one's number.

Losing the file still degrades to "probably fine" rather than "reuses from 1", because the
clock remains a floor.

**Persisted before it is returned, never after.** The caller may crash, or the network may
swallow the write, between being handed a nonce and the server seeing it. If the record
were written after a successful send, that gap would hand the same number out twice. It is
written first and fsynced, so a nonce that was *never used* is simply skipped — and skipped
nonces cost nothing, while a repeat is a rejected write the caller cannot explain.

The file is rewritten whole under `os.replace`, which is atomic on POSIX and on Windows for
the same path: a reader either sees the old file or the new one, never a truncated one. The
temporary it is renamed from carries the pid, because a fixed `.tmp` name is a second way two
processes collide — one writing it while the other renames it — which is the same defect as
the nonce race wearing different clothes.

The containing directory is fsynced too, because on ext4 the rename can otherwise outlive the
data it points at across a power cut.

**POSIX.** `fcntl.flock` and fsyncing a directory handle are both POSIX; this module does not
run on Windows and says so here rather than failing at import with a message about a missing
attribute. That was already true of the directory fsync before the lock was added.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from .durable import fsync_dir, mkdir_durable

# The highest nonce this *process* has issued, across every store object and every pair.
# A store rebuilt from a deleted file knows nothing, and the clock alone cannot separate
# two allocations landing in the same millisecond — so the one thing still available, the
# fact that this process already handed out that number, is kept here. It bounds the
# realistic loss case (a file removed while the program runs, or a second store opened on
# the same path) to something strictly increasing.
#
# What it does not cover, and no in-process value could: file lost AND a new process AND
# the same millisecond as the last write. That needs the persisted state, which is the
# point of the file; the residue is one refused write and a retry that succeeds a
# millisecond later.
_process_floor = 0


class NonceStore:
    """Per-(did, room) allocation, persisted before each number is handed out."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._state: dict[str, dict[str, int]] = self._load()

    def _load(self) -> dict[str, dict[str, int]]:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            # A missing file is the ordinary first run. A corrupt one is treated the same
            # way on purpose: the clock floor below keeps a lost file safe, whereas
            # refusing to start would strand a caller over state it cannot repair.
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, int]] = {}
        for did, rooms in raw.items():
            if isinstance(rooms, dict):
                out[did] = {r: n for r, n in rooms.items() if isinstance(n, int) and n >= 0}
        return out

    def _lock(self):
        """Exclusive lock over the whole read-modify-write.

        A sidecar file rather than the state file itself: the state file is replaced by
        `os.replace`, so a lock held on it would follow the old inode and stop excluding
        anyone the moment the first writer finished.
        """
        mkdir_durable(self._path.parent)
        fd = os.open(str(self._path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _flush(self) -> None:
        mkdir_durable(self._path.parent)
        tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
        # The rename itself needs to be durable, not just the bytes it points at.
        fsync_dir(self._path.parent)

    def last(self, did: str, room: str) -> int | None:
        """The last nonce allocated for this pair, or None if there has never been one."""
        return self._state.get(did, {}).get(room)

    def allocate(self, did: str, room: str) -> int:
        """Reserve and persist the next nonce for `did` in `room`, then return it."""
        global _process_floor
        fd = self._lock()
        try:
            return self._allocate_locked(did, room)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _allocate_locked(self, did: str, room: str) -> int:
        global _process_floor
        # Re-read inside the lock. The copy loaded at construction is stale the moment another
        # process allocates, and allocating from it is exactly the duplicate this lock exists to
        # prevent — taking the lock and then trusting memory would be a lock that guards nothing.
        self._state = self._load()
        known = self._state.get(did, {}).get(room)
        # With no record, the floor is the clock PLUS ONE, not the clock. A nonce equal to
        # the current millisecond may already have been used and forgotten — that is exactly
        # the state after a lost file — and the server's rule is strictly greater, so
        # returning `now` would be refused with nothing local to explain it. The test for
        # this failed before the `+ 1` was here.
        previous = known if known is not None else int(time.time() * 1000)
        # `max` and not `previous + 1`: a clock that has moved forward since the last write
        # jumps the counter, which keeps two processes sharing one key from colliding on a
        # value neither has persisted yet.
        nonce = max(int(time.time() * 1000), previous + 1, _process_floor + 1)
        _process_floor = nonce
        self._state.setdefault(did, {})[room] = nonce
        self._flush()
        return nonce
