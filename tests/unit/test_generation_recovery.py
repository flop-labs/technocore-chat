"""A committed first record activates one durable generation reservation."""

import errno
import os
import time
from pathlib import Path

import orjson
import pytest

import store


def _age_and_reap(root, room):
    path = store.room_path(root, room)
    now = time.time()
    old = now - store.IDLE_SECONDS - 60
    os.utime(path, (old, old))
    store._reap_pass(root, now)
    return path


@pytest.mark.parametrize("recreate", [False, True], ids=["first-create", "recreate"])
@pytest.mark.parametrize("partial", [None, b"", b'{"seq":'], ids=["absent", "empty", "torn"])
def test_failed_first_record_reuses_its_generation_from_durable_state(
    tmp_path, monkeypatch, recreate, partial
):
    room = "recovering"
    if recreate:
        for i in range(3):
            store.append(tmp_path, room, "bot", str(i))
        _age_and_reap(tmp_path, room)
    floor, generation = store.last_seq(tmp_path, room), store.room_generation(tmp_path, room)
    path = store.room_path(tmp_path, room)
    shard = store._seq_state_path(tmp_path, room)
    real_open = Path.open

    def fail_first_record(target, mode="r", *args, **kwargs):
        if target == path and mode == "ab":
            if partial is not None:
                target.write_bytes(partial)
            raise OSError(errno.EIO, "first record did not commit")
        return real_open(target, mode, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", fail_first_record)
        for _ in range(2):
            with pytest.raises(OSError):
                store.append(tmp_path, room, "bot", "failed")
            assert store.room_generation(tmp_path, room) == generation
            assert store.last_seq(tmp_path, room) == floor
    reserved = orjson.loads(shard.read_bytes())[room]
    assert reserved["gen"] == generation + 1
    assert reserved["start"] == floor + 1
    # Recovery has no process-local pending queue. Rebuild the only seq-state cache.
    store._SEQ_CHECKED.clear()
    first = store.append(tmp_path, room, "bot", "accepted")
    assert first["seq"] == floor + 1
    assert store.read_messages(tmp_path, room)["generation"] == generation + 1
    assert store.append(tmp_path, room, "bot", "again")["seq"] == floor + 2
    with store._locked(path):
        store._compact(path, keep=1)
    assert store.room_generation(tmp_path, room) == generation + 1
    assert not _age_and_reap(tmp_path, room).exists()
    assert store.room_generation(tmp_path, room) == generation + 1
    assert "start" not in orjson.loads(shard.read_bytes())[room]
    assert store.append(tmp_path, room, "bot", "next conversation")["seq"] == floor + 3
    assert store.room_generation(tmp_path, room) == generation + 2


@pytest.mark.parametrize("committed", [False, True], ids=["before-replace", "after-replace"])
def test_failed_reservation_never_publishes_a_message_or_double_bumps(
    tmp_path, monkeypatch, committed
):
    room = "reservation"
    for i in range(3):
        store.append(tmp_path, room, "bot", str(i))
    path = _age_and_reap(tmp_path, room)
    shard = store._seq_state_path(tmp_path, room)
    replace = store._replace

    def fail(target, *args, **kwargs):
        if target == shard:
            if committed:
                replace(target, *args, **kwargs)
            raise OSError(errno.EIO, "reservation acknowledgement failed")
        return replace(target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(store, "_replace", fail)
        with pytest.raises(OSError):
            store.append(tmp_path, room, "bot", "not written")
    assert not path.exists()
    assert store.last_seq(tmp_path, room) == 3
    assert store.room_generation(tmp_path, room) == 1
    store._SEQ_CHECKED.clear()
    assert store.append(tmp_path, room, "bot", "recovered")["seq"] == 4
    assert store.room_generation(tmp_path, room) == 2
    assert store.append(tmp_path, room, "bot", "later")["seq"] == 5
    assert not _age_and_reap(tmp_path, room).exists()
    assert store.room_generation(tmp_path, room) == 2


def test_a_landed_record_activates_the_epoch_despite_a_later_failure(tmp_path, monkeypatch):
    room = "landed"
    with monkeypatch.context() as fault:
        fault.setattr(store, "_ring_limit", lambda _root: 1)

        def fail(*args, **kwargs):
            raise OSError(errno.EIO, "compaction failed after the append")

        fault.setattr(store, "_compact", fail)
        with pytest.raises(OSError):
            store.append(tmp_path, room, "bot", "on disk")
    store._SEQ_CHECKED.clear()
    assert store.room_generation(tmp_path, room) == 1
    assert store.read_messages(tmp_path, room)["messages"][0]["text"] == "on disk"
    assert store.append(tmp_path, room, "bot", "later")["seq"] == 2
    assert store.room_generation(tmp_path, room) == 1


def test_a_failed_unlink_does_not_turn_a_later_append_into_a_recreation(tmp_path, monkeypatch):
    room = "not-recreated"
    for i in range(3):
        store.append(tmp_path, room, "bot", str(i))
    path = store.room_path(tmp_path, room)
    unlink = Path.unlink

    def fail(target, *args, **kwargs):
        if target == path:
            raise OSError(errno.EIO, "room still exists")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", fail)
        _age_and_reap(tmp_path, room)
    assert path.exists()
    # Prevent append's maintenance from retrying the intentionally failed reap first.
    (tmp_path / ".reaped").touch()
    assert store.append(tmp_path, room, "bot", "same conversation")["seq"] == 4
    assert store.room_generation(tmp_path, room) == 1
    assert not _age_and_reap(tmp_path, room).exists()
    assert store.room_generation(tmp_path, room) == 1
    assert store.append(tmp_path, room, "bot", "new conversation")["seq"] == 5
    assert store.room_generation(tmp_path, room) == 2


@pytest.mark.parametrize("start", [None, "1", -1, True, [], {}])
def test_malformed_reservations_do_not_change_legacy_generations(tmp_path, start):
    room = "legacy"
    store._seq_state_path(tmp_path, room).write_bytes(
        orjson.dumps({room: {"floor": 0, "gen": 7, "start": start}})
    )
    assert store.room_generation(tmp_path, room) == 7


def test_existing_appends_do_not_read_or_rewrite_sequence_metadata(tmp_path, monkeypatch):
    room = "hot-room"
    store.append(tmp_path, room, "bot", "first")
    (tmp_path / ".reaped").touch()
    shard = store._seq_state_path(tmp_path, room)
    real_open = Path.open

    def no_shard_io(path, *args, **kwargs):
        assert path != shard, "an existing append added sequence metadata I/O"
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", no_shard_io)
        assert store.append(tmp_path, room, "bot", "second")["seq"] == 2
