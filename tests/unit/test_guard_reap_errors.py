"""A failed room stat must not make its ownership notes look orphaned."""

import errno
import os
import time
from pathlib import Path

import pytest

import store


def _age(path):
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(path, (old, old))


def _reap(root):
    (root / ".reaped").unlink(missing_ok=True)
    store._reap(root)


@pytest.mark.parametrize("ns", store.ROOM_GUARD_NS)
@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES])
def test_room_stat_error_keeps_guard_and_continues_reaping(tmp_path, monkeypatch, ns, error):
    store.append(tmp_path, "d-live", "bot", "still active")
    room = store.room_path(tmp_path, "d-live")
    store.note_set(tmp_path, ns, "d-live", "guard-value")
    guard = store.note_path(tmp_path, ns, "d-live")
    _age(guard)
    store.note_set(tmp_path, "ordinary", "stale", "expired")
    unrelated = store.note_path(tmp_path, "ordinary", "stale")
    _age(unrelated)

    real_room_path, real_stat = store.room_path, Path.stat
    real_walk = store._walk
    note_order = []
    armed = False
    failures = 0

    def guard_first(d, suffix):
        entries = real_walk(d, suffix)
        if Path(d) == tmp_path / "notes" and suffix == ".txt":
            # Cleanup before the failed guard would not prove the pass continues.
            for entry in sorted(entries, key=lambda entry: entry.path != str(guard)):
                note_order.append(entry.path)
                yield entry
        else:
            yield from entries

    def resolve_then_fail(root, name):
        nonlocal armed
        path = real_room_path(root, name)
        # Resolution has successfully found the live room. Fail the subsequent
        # filesystem operation that reads its mtime, then let the filesystem recover.
        if path == room:
            armed = True
        return path

    def stat(path, *args, **kwargs):
        nonlocal armed, failures
        if armed and path == room:
            armed = False
            failures += 1
            raise OSError(error, os.strerror(error), str(path))
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_walk", guard_first)
        patch.setattr(store, "room_path", resolve_then_fail)
        patch.setattr(Path, "stat", stat)
        _reap(tmp_path)

    assert failures == 1
    assert store.note_get(tmp_path, ns, "d-live") == "guard-value"
    assert room.exists()
    assert not unrelated.exists()  # one unknown room must not abort other cleanup
    assert note_order == [str(guard), str(unrelated)]
    assert store.note_stats(tmp_path)["total"] == 1
    assert store.note_stats(tmp_path)["bytes"] == len(b"guard-value")

    # Recovery does not delay the decision forever: the live room still keeps its
    # guard, and an ordinary later pass removes both once the room really expires.
    _reap(tmp_path)
    assert guard.exists()
    _age(room)
    _reap(tmp_path)
    assert not room.exists()
    assert not guard.exists()
    assert store.note_stats(tmp_path)["total"] == 0


@pytest.mark.parametrize("ns", store.ROOM_GUARD_NS)
@pytest.mark.parametrize("room_state", ["live", "missing", "disappears"])
def test_guard_reap_still_distinguishes_live_and_missing_rooms(
    tmp_path, monkeypatch, ns, room_state
):
    store.append(tmp_path, "d-control", "bot", "active")
    room = store.room_path(tmp_path, "d-control")
    store.note_set(tmp_path, ns, "d-control", "guard-value")
    guard = store.note_path(tmp_path, ns, "d-control")
    _age(guard)
    if room_state == "missing":
        room.unlink()
    elif room_state == "disappears":
        real_room_path = store.room_path

        def resolve_then_remove(root, name):
            path = real_room_path(root, name)
            if path == room:
                path.unlink(missing_ok=True)
            return path

        monkeypatch.setattr(store, "room_path", resolve_then_remove)

    _reap(tmp_path)
    assert guard.exists() == (room_state == "live")
    assert store.note_stats(tmp_path)["total"] == int(room_state == "live")
