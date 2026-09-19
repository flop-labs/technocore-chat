"""Filesystem durability primitives shared by store write paths."""

import os
from pathlib import Path

import config

_synced_room_directories: set[Path] = set()


def fsync_parent(path: Path, *, permission_boundary: bool = False) -> bool:
    """Persist ``path`` in its parent; return False only at an allowed permission boundary.

    ``O_DIRECTORY`` is POSIX-only. This service already requires ``fcntl``; a native
    Windows port needs the equivalent directory-handle primitive before using this module.
    """
    try:
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except PermissionError:
        if permission_boundary:
            return False
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def fsync_ancestors(path: Path, *, strict_first: bool = False) -> None:
    """Persist a visible directory chain up to its mount or provisioning boundary."""
    path = path.resolve()
    while path != path.parent:
        parent = path.parent
        if path.stat().st_dev != parent.stat().st_dev:
            break
        if not fsync_parent(path, permission_boundary=not strict_first):
            config._dbg(2, "fsync_boundary")
            break  # a searchable pre-existing ancestor is the operator's boundary
        path, strict_first = parent, False


def sync_room_entry(root: Path, path: Path, *, root_was_missing: bool = False) -> None:
    """Persist a legacy or sharded room entry and repair its root chain once."""
    rooms = (root / "rooms").resolve()
    parent = path.parent.resolve()
    fsync_parent(path)
    if parent != rooms:
        fsync_parent(parent)
    if rooms not in _synced_room_directories:
        fsync_parent(rooms)
        fsync_ancestors(rooms.parent, strict_first=root_was_missing)
        _synced_room_directories.add(rooms)
