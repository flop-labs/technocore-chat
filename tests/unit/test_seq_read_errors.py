"""An I/O failure must not become an empty sequence-state map that gets persisted."""

import errno
import os
import time
from pathlib import Path

import orjson
import pytest

import store


@pytest.fixture
def paired_rooms(tmp_path):
    # Real names and real sharding: a failed update must preserve its neighbors too.
    seen = {}
    for i in range(257):
        room = f"room{i}"
        shard = store._shard(room)
        if shard in seen:
            first, second = seen[shard], room
            break
        seen[shard] = room
    path = store._seq_state_path(tmp_path, first)
    state = {first: {"floor": 55, "gen": 4}, second: {"floor": 91, "gen": 7}}
    path.write_bytes(orjson.dumps(state))
    return first, second, path, state


def _fail_read(monkeypatch, path, error, *, entry=False):
    method = "open" if entry else "read_bytes"
    original = getattr(Path, method)

    def failing(self, *args, **kwargs):
        if self == path:
            raise OSError(error, "temporary sequence-state read failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES, errno.EMFILE])
@pytest.mark.parametrize("entry", [False, True], ids=["whole-map", "generation-entry"])
def test_failed_seq_update_preserves_every_room(tmp_path, monkeypatch, paired_rooms, error, entry):
    first, second, path, state = paired_rooms
    before = path.read_bytes()
    with monkeypatch.context() as fault:
        _fail_read(fault, path, error, entry=entry)
        with pytest.raises(OSError) as raised:
            store._set_seq_entry(tmp_path, first, 100)
        assert raised.value.errno == error
    assert path.read_bytes() == before
    assert store.last_seq(tmp_path, second) == 91
    assert store.room_generation(tmp_path, second) == 7
    # The failure is temporary: retry updates the target without dropping its neighbor.
    store._set_seq_entry(tmp_path, first, 100)
    assert store.last_seq(tmp_path, first) == 100
    assert store.room_generation(tmp_path, first) == 4
    assert orjson.loads(path.read_bytes())[second] == state[second]


@pytest.mark.parametrize("error", [errno.EIO, errno.EACCES, errno.EMFILE])
@pytest.mark.parametrize("failed_file", ["legacy", "shard"])
@pytest.mark.parametrize("recovered", [False, True], ids=["first-split", "old-worker-map"])
def test_failed_split_keeps_its_source_and_existing_shard(
    tmp_path, monkeypatch, paired_rooms, error, failed_file, recovered
):
    first, second, shard, state = paired_rooms
    legacy = store._seq_state_path(tmp_path)
    incoming = {"floor": 100, "gen": 9} if recovered else {"floor": 20, "gen": 2}
    legacy.write_bytes(orjson.dumps({first: incoming}))
    backup = legacy.with_suffix(".pre-shard")
    if recovered:
        backup.write_bytes(b'{"original":{"floor":1,"gen":1}}')
    original_backup = backup.read_bytes() if recovered else None
    source, target = legacy.read_bytes(), shard.read_bytes()
    with monkeypatch.context() as fault:
        _fail_read(fault, legacy if failed_file == "legacy" else shard, error)
        store._split_seq_state(tmp_path)
    assert legacy.exists(), "an unreadable map must stay available for retry"
    assert legacy.read_bytes() == source
    assert shard.read_bytes() == target
    assert (backup.read_bytes() if backup.exists() else None) == original_backup
    store._split_seq_state(tmp_path)
    assert not legacy.exists()
    assert backup.read_bytes() == (original_backup if recovered else source)
    assert orjson.loads(shard.read_bytes()) == (state | {first: incoming} if recovered else state)
    assert store.last_seq(tmp_path, second) == 91


@pytest.mark.parametrize("legacy", [False, True], ids=["shard", "legacy-fallback"])
def test_an_unreadable_floor_never_restarts_a_recreated_room(tmp_path, monkeypatch, legacy):
    room = "returning"
    path = store._seq_state_path(tmp_path, "" if legacy else room)
    path.write_bytes(orjson.dumps({room: {"floor": 100, "gen": 8}}))
    # Keep maintenance out of this operation so the failure is the append's floor read.
    # A cap of one also checks that the refused append returns its reservation.
    monkeypatch.setattr(store, "MAX_ROOMS", 1)
    (tmp_path / ".reaped").touch()
    with monkeypatch.context() as fault:
        _fail_read(fault, path, errno.EIO, entry=True)
        with pytest.raises(OSError) as raised:
            store.append(tmp_path, room, "bot", "after the reap")
        assert raised.value.errno == errno.EIO
    assert not store.room_path(tmp_path, room).exists()
    assert store.last_seq(tmp_path, room) == 100
    assert store.room_generation(tmp_path, room) == 8
    assert store.append(tmp_path, room, "bot", "after recovery")["seq"] == 101
    assert store.room_generation(tmp_path, room) == 9


@pytest.mark.parametrize("failure", ["read", "replace"])
def test_the_reaper_keeps_a_room_until_its_floor_is_recorded(tmp_path, monkeypatch, failure):
    room = "kept-for-retry"
    for i in range(3):
        store.append(tmp_path, room, "bot", f"retained {i}")
    path = store.room_path(tmp_path, room)
    shard = store._seq_state_path(tmp_path, room)
    before = path.read_bytes()
    now = time.time()
    stale = now - store.IDLE_SECONDS - 60
    os.utime(path, (stale, stale))
    with monkeypatch.context() as fault:
        if failure == "read":
            _fail_read(fault, shard, errno.EIO)
        else:
            replace = store._replace

            def failed_replace(target, *args, **kwargs):
                if target == shard:
                    raise OSError(errno.EIO, "temporary sequence-state write failure")
                return replace(target, *args, **kwargs)

            fault.setattr(store, "_replace", failed_replace)
        store._reap_pass(tmp_path, now)
    assert path.exists(), "deleting before the floor lands restarts the next generation at 1"
    assert path.read_bytes() == before
    assert store.counters(tmp_path)["reaped_idle"] == 0
    store._reap_pass(tmp_path, now)
    assert not path.exists()
    assert store.last_seq(tmp_path, room) == 3
    assert store.room_generation(tmp_path, room) == 1
    assert store.counters(tmp_path)["reaped_idle"] == 1
    assert store.append(tmp_path, room, "bot", "after recovery")["seq"] == 4
    assert store.room_generation(tmp_path, room) == 2


def test_a_generation_update_failure_precedes_the_message_write(tmp_path, monkeypatch):
    room = "written"
    shard = store._seq_state_path(tmp_path, room)
    replace = store._replace

    def failed_replace(path, *args, **kwargs):
        if path == shard:
            raise OSError(errno.EIO, "temporary sequence-state write failure")
        return replace(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(store, "_replace", failed_replace)
        with pytest.raises(OSError):
            store.append(tmp_path, room, "bot", "not committed")
    assert not store.room_path(tmp_path, room).exists()
    assert store.room_generation(tmp_path, room) == 0
    posted = store.append(tmp_path, room, "bot", "after recovery")
    assert posted["seq"] == 1
    assert store.read_messages(tmp_path, room)["messages"] == [posted]
    assert store.room_generation(tmp_path, room) == 1
