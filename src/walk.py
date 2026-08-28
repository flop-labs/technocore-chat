"""Directory walks that refuse to follow substituted child symlinks.

`store._walk` used to classify a directory and then reopen it by pathname. A
real child could be replaced by a directory symlink in that window, after which
the reaper unlinked files outside the store root. POSIX child directories are
opened with `O_NOFOLLOW` relative to the parent fd, so that substitution raises
and is skipped. The configured root itself may be a symlink (a mounted volume)
and is followed once. Windows has no `dir_fd` openat, so observed child
symlinks and junctions are skipped; hostile same-privilege rename races stay
out of scope.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path

# Rooms are one level down, notes two, plus a spare for a foreign extra layer.
# Deeper trees are skipped rather than walked until fds or the stack run out.
MAX_DEPTH = 8
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FD_SCANDIR = os.name != "nt"

# Tests assign a callable(full_path, name) that runs after classification and
# before the child directory is opened — the substitution window.
_before_open_child = None


class FileEntry:
    """A suffix file found under a walk. `path` is reconstructed, never followed."""

    __slots__ = ("name", "path")

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path

    def stat(self, follow_symlinks: bool = False) -> os.stat_result:
        return os.stat(self.path, follow_symlinks=follow_symlinks)


def files(d: Path | str, suffix: str) -> Iterator[FileEntry]:
    """Every `*suffix` file under `d`, skipping child directory symlink substitutions."""
    yield from _walk(os.fspath(d), suffix, None, None, 0)


def counts(d: Path | str, suffix: str, sized: bool = False) -> tuple[int, int]:
    """(count, total bytes) of `*suffix` files under `d`, same confinement as `files`."""
    n = 0
    size = 0
    for entry in files(d, suffix):
        n += 1
        if sized:
            try:
                size += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return n, size


def _is_link(path: str) -> bool:
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction is not None and is_junction(path))


def _open_dir(name: str, dir_fd: int | None, full_path: str, *, child: bool) -> int | None:
    if child and _before_open_child is not None:
        _before_open_child(full_path, name)
    if child and dir_fd is not None and _NOFOLLOW and _FD_SCANDIR:
        return os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)
    if child and _is_link(full_path):
        raise OSError(errno.ELOOP, "directory symlink", full_path)
    if not _FD_SCANDIR:
        return None
    flags = os.O_RDONLY | _DIRECTORY
    try:
        return os.open(full_path, flags)
    except OSError:
        return os.open(full_path, os.O_RDONLY)


def _walk(
    path: str, suffix: str, dir_fd: int | None, name: str | None, depth: int
) -> Iterator[FileEntry]:
    if depth > MAX_DEPTH:
        return
    fd = None
    try:
        fd = _open_dir(
            path if name is None else name,
            dir_fd,
            path,
            child=name is not None,
        )
        # scandir does not take ownership of a directory fd.
        with os.scandir(fd if fd is not None else path) as entries:
            for entry in entries:
                full = os.path.join(path, entry.name)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    yield from _walk(full, suffix, fd, entry.name, depth + 1)
                elif entry.name.endswith(suffix):
                    yield FileEntry(entry.name, full)
    except OSError:
        return
    finally:
        if fd is not None:
            os.close(fd)
