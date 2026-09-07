"""Run: uv run --group dev python -m pytest tests

A room's floor and generation lived in one root-level map that was read and parsed IN FULL on
every room read — `read_messages` asks for the generation — and rewritten in full under one
global lock on every create and every reap. Nothing ever removed an entry, so it grew with
every room the service had ever reaped: 3.2 MB and 42 ms per request at the ~90k of a live
deployment, which is 99% of a read (#489).

It is sharded 256 ways now, by the same `_shard` that resolves a room's own bucket. Three
things have to hold: the split must not lose or reorder a floor or a generation, a read must
answer identically before, during and after it, and the shards must stay out of everything
that walks the store.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import orjson
import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _legacy(root: Path, entries: dict) -> None:
    """A pre-shard map, exactly as builds before this change wrote it — no `t` on any entry."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".seqstate").write_bytes(orjson.dumps(entries))


def _reap_now(root: Path) -> None:
    import store

    (root / ".reaped").unlink(missing_ok=True)  # the throttle would skip the pass otherwise
    store._reap(root)


# --------------------------------------------------------------------------- the split


def test_the_split_preserves_every_floor_and_generation(tmp_path) -> None:
    """The migration is the one step that can silently lose #139's whole point: a floor that
    does not survive strands every cursor past it, and a generation that does not survive
    tells a stateful reader the conversation is unchanged when it is not."""
    import store

    before = {f"gone{i}": {"floor": i * 3, "gen": i} for i in range(400)}
    _legacy(tmp_path, before)
    _reap_now(tmp_path)

    for room, entry in before.items():
        assert store.last_seq(tmp_path, room) == entry["floor"], room
        assert store.room_generation(tmp_path, room) == entry["gen"], room


def test_the_split_files_each_room_in_its_own_shard(tmp_path) -> None:
    """The layout claim, not merely that the values survived: an entry must land in the shard
    a *reader* will look in, which is the one `_shard` names — the same function that resolves
    the room's bucket, so the two never drift apart."""
    import store

    _legacy(tmp_path, {f"gone{i}": {"floor": i, "gen": 1} for i in range(200)})
    _reap_now(tmp_path)

    for i in range(200):
        room = f"gone{i}"
        shard = tmp_path / f".seqstate.{store._shard(room)}"
        assert room in store._read_seq_state(shard), f"{room} is not in the shard reads use"


def test_the_split_retires_the_old_map_by_renaming_it(tmp_path) -> None:
    """Renamed, never unlinked: a downgrade puts the old code back in front of a map it still
    understands. And the retired name must not look like a shard — the sweep and every future
    reader glob `.seqstate.??`, which two hex characters match and `pre-shard` does not."""
    _legacy(tmp_path, {"gone": {"floor": 7, "gen": 2}})
    _reap_now(tmp_path)

    assert not (tmp_path / ".seqstate").exists(), "the old map is still being read"
    kept = tmp_path / ".seqstate.pre-shard"
    assert orjson.loads(kept.read_bytes()) == {"gone": {"floor": 7, "gen": 2}}
    assert kept not in set(tmp_path.glob(".seqstate.??")), "the backup is globbed as a shard"


def test_the_split_is_a_no_op_once_it_has_run(tmp_path) -> None:
    """It rides the reaper, so it runs on every pass for the life of the store. It has to cost
    nothing after the first, and — the part that would actually corrupt — it must never
    resurrect an entry a later write replaced."""
    import store

    _legacy(tmp_path, {"gone": {"floor": 9, "gen": 1}})
    _reap_now(tmp_path)
    store._write_record(tmp_path, "gone", "bot", "back")  # recreated: floor clears, gen bumps
    after = store.room_generation(tmp_path, "gone")

    for _ in range(3):
        _reap_now(tmp_path)
    assert store.room_generation(tmp_path, "gone") == after == 2
    # 10, not 1: the migrated floor of 9 is what the recreated room continues from, which is
    # the whole of #139 dir #2 — a cursor at 9 sees the new message instead of starving.
    assert store.last_seq(tmp_path, "gone") == 10, "the split lost the floor it carried over"


def test_a_shard_entry_wins_the_split_against_the_old_map(tmp_path, monkeypatch) -> None:
    """A create landing while the split is in flight writes the shard; the split must not
    then overwrite it with the older value it read from the map. Driven from inside the
    split's own read rather than timed to it."""
    import store

    _legacy(tmp_path, {"gone": {"floor": 50, "gen": 1}})
    real_read = store._read_seq_state
    raced = []

    def write_a_newer_fact_mid_split(path):
        state = real_read(path)
        if path.name == ".seqstate" and not raced:
            raced.append(True)
            real_set(tmp_path, "gone", 99)  # lands in the shard, after the map was read
        return state

    real_set = store._set_seq_entry
    monkeypatch.setattr(store, "_read_seq_state", write_a_newer_fact_mid_split)
    _reap_now(tmp_path)
    monkeypatch.undo()

    assert raced, "premise: a write landed inside the split's window"
    assert store.last_seq(tmp_path, "gone") == 99, "the split clobbered a newer shard entry"


def test_a_map_written_after_the_split_wins_over_the_shard(tmp_path) -> None:
    """The mixed-version window, and the case where the merge has to go the *other* way.

    A worker still running the pre-shard code writes `.seqstate` — recreating a file this
    change has already consumed and renamed. That entry is newer than the shard's by
    construction, so a later split must take it. Letting the shard win instead drops that
    worker's reap: the room's floor regresses and every cursor past it misses messages.
    """
    import store

    _legacy(tmp_path, {"gone": {"floor": 5, "gen": 1}})
    _reap_now(tmp_path)
    assert (tmp_path / ".seqstate.pre-shard").exists(), "premise: the first split has run"

    _legacy(tmp_path, {"gone": {"floor": 500, "gen": 9}})  # an old worker, post-split
    _reap_now(tmp_path)

    assert store.last_seq(tmp_path, "gone") == 500, "the old worker's floor was dropped"
    assert store.room_generation(tmp_path, "gone") == 9, "its generation bump was dropped"
    assert not (tmp_path / ".seqstate").exists(), "the recovered map was not consumed"
    kept = orjson.loads((tmp_path / ".seqstate.pre-shard").read_bytes())
    assert kept == {"gone": {"floor": 5, "gen": 1}}, "the backup lost the original state"


# --------------------------------------------------------------------------- reads


def test_a_read_answers_from_the_old_map_until_the_split_runs(tmp_path) -> None:
    """The window between deploying this and the first reap. A read in it must be correct, or
    the change needs a flag day instead of riding the reaper."""
    import store

    _legacy(tmp_path, {"gone": {"floor": 12, "gen": 3}})
    assert not list(tmp_path.glob(".seqstate.??")), "premise: nothing is sharded yet"
    assert store.last_seq(tmp_path, "gone") == 12
    assert store.room_generation(tmp_path, "gone") == 3


def test_a_read_never_parses_the_whole_map_once_the_split_has_run(tmp_path) -> None:
    """The cost claim, as bytes read rather than as a duration. The old read pulled in every
    entry in the store; a sharded one must pull in its own shard and nothing else."""
    import store

    _legacy(tmp_path, {f"gone{i}": {"floor": i, "gen": 1} for i in range(5_000)})
    _reap_now(tmp_path)

    read = []
    real_read_bytes = Path.read_bytes

    def counting(self):
        data = real_read_bytes(self)
        if self.name.startswith(".seqstate"):
            read.append(len(data))
        return data

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_bytes", counting)
        store.room_generation(tmp_path, "gone7")
    whole = (tmp_path / ".seqstate.pre-shard").stat().st_size
    assert sum(read) < whole / 50, f"read {sum(read)}B of a {whole}B map — this is not sharded"


def test_a_generation_survives_the_split_and_keeps_counting_up(tmp_path) -> None:
    """The regression that would make the split invisible in tests and wrong in production: if
    a create read the generation from the shard only, a room whose state had not been split yet
    would restart at 1 and a stateful reader would be told nothing changed (#139 dir #3)."""
    import store

    _legacy(tmp_path, {"gone": {"floor": 40, "gen": 6}})
    store._write_record(tmp_path, "gone", "bot", "back")  # before any split has run

    assert store.room_generation(tmp_path, "gone") == 7, "the generation restarted"
    # And the floor from the unsplit map was honoured too: 41 continues 40, it does not restart.
    assert store.last_seq(tmp_path, "gone") == 41, "the pre-split floor was not carried over"


# --------------------------------------------------------------------------- isolation


def test_shards_are_never_walked_counted_or_reaped_as_rooms(tmp_path) -> None:
    """They sit at the root beside `.counters` and `.usage`, so the room caps, the listings and
    the bucket pruning must not see them. A per-room sidecar in the bucket — the other obvious
    layout — would have kept every bucket a reaped room ever used permanently non-empty."""
    import store

    _legacy(tmp_path, {f"gone{i}": {"floor": i, "gen": 1} for i in range(300)})
    store._write_record(tmp_path, "live", "bot", "hi")
    _reap_now(tmp_path)

    assert len(list(tmp_path.glob(".seqstate.??"))) > 1, "premise: the store is sharded"
    assert store._count_rooms(tmp_path)[0] == 1, "shards counted against the room cap"
    assert store.list_rooms(tmp_path) == ["live"], "shards listed as rooms"
    assert [e.name for e in store._walk(tmp_path / "rooms", ".jsonl")] == ["live.jsonl"]


def test_creates_in_different_shards_do_not_serialise_on_one_lock(tmp_path) -> None:
    """The write half of #489, as a topology rather than a duration: the map was rewritten
    under ONE lock per create, so four creates could never be inside it at once. Under the old
    shape this fails by timeout rather than by a flaky millisecond count."""
    import store

    store._write_record(tmp_path, "seed", "bot", "hi")
    (tmp_path / ".reaped").touch()
    rooms = _four_distinct_shards(store)
    together = threading.Barrier(len(rooms))
    real_set = store._set_seq_entry
    failed = []

    def wait_inside_the_seq_write(root, room, floor):
        if room in rooms:
            together.wait(timeout=10)
        return real_set(root, room, floor)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_set_seq_entry", wait_inside_the_seq_write)

        def create(room):
            try:
                store._write_record(tmp_path, room, "bot", "hi")
            except BaseException as exc:  # noqa: BLE001 - surfaced through `failed`
                failed.append(exc)

        threads = [threading.Thread(target=create, args=(r,)) for r in rooms]
        [t.start() for t in threads]
        [t.join(30) for t in threads]

    assert not failed, f"creates in distinct shards could not overlap: {failed}"
    assert store._count_rooms(tmp_path)[0] == len(rooms) + 1


def _four_distinct_shards(store) -> list[str]:
    """Four room names that hash into four different shards, so the barrier is testing the
    lock split and not four names that happen to share a file."""
    seen: dict[str, str] = {}
    for i in range(500):
        room = f"room{i}"
        seen.setdefault(store._shard(room), room)
        if len(seen) == 4:
            break
    return list(seen.values())


def test_a_torn_or_hand_edited_shard_reads_as_never_existed(tmp_path) -> None:
    """Both fields are read on the request path, so garbage must degrade to 0 — which is
    exactly `room_generation`'s documented "never existed" — and never raise a 500."""
    import store

    shard = tmp_path / f".seqstate.{store._shard('odd')}"
    for junk in (b"{", b"[]", b'{"odd": 3}', b'{"odd": {"floor": "x", "gen": -2}}'):
        shard.write_bytes(junk)
        assert store.last_seq(tmp_path, "odd") == 0, junk
        assert store.room_generation(tmp_path, "odd") == 0, junk


def test_the_entry_carries_when_it_was_written(tmp_path) -> None:
    """`t` is unused today and is here so the reclaim half of #489 needs no second migration to
    date what it finds. If it stops being written, that follow-up silently cannot bound
    anything written in the meantime."""
    import time

    import store

    store._write_record(tmp_path, "dated", "bot", "hi")
    entry = store._read_seq_state(tmp_path / f".seqstate.{store._shard('dated')}")["dated"]
    assert isinstance(entry["t"], int)
    assert abs(entry["t"] - time.time()) < 60, "the stamp is not this write's own clock"


def test_the_shard_of_a_name_is_the_shard_of_its_room_bucket(tmp_path) -> None:
    """One resolver for both, so a re-shard can never move a room's data without moving its
    floor with it. `_shard` is a frozen on-disk format; this is what pins the two together."""
    import store

    for room in ("lobby", "p-secret", "mb-inbox", "e-fast", "z9"):
        bucket = store.room_path(tmp_path, room).parent.name
        assert store._seq_state_path(tmp_path, room).name == f".seqstate.{bucket}"
    assert store._seq_state_path(tmp_path).name == ".seqstate", "no room names the old map"


def test_seq_state_survives_a_read_only_store(tmp_path) -> None:
    """Best effort, like `_bump`: the caller's write has already succeeded by the time the
    floor is recorded, so an unwritable shard must not turn that success into a 500."""
    import store

    store._write_record(tmp_path, "fine", "bot", "hi")
    shard = tmp_path / f".seqstate.{store._shard('nope')}"
    shard.mkdir()  # a directory where the shard file goes: every write to it fails
    store._set_seq_entry(tmp_path, "nope", 5)  # must not raise
    assert store.last_seq(tmp_path, "nope") == 0
    assert os.path.isdir(shard)
