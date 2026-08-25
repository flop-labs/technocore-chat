"""Blocking exclusive file locks with the same contract on POSIX and Windows."""

from __future__ import annotations

import errno
import os
import time
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_RETRY_SECONDS = 0.01


def acquire(file: BinaryIO) -> None:
    """Block until this process exclusively owns the file's lock domain."""
    if os.name != "nt":
        fcntl.flock(file, fcntl.LOCK_EX)
        return
    while True:
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                raise
            time.sleep(_RETRY_SECONDS)


def release(file: BinaryIO) -> None:
    """Release a lock previously obtained by :func:`acquire`."""
    if os.name != "nt":
        fcntl.flock(file, fcntl.LOCK_UN)
        return
    file.seek(0)
    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
