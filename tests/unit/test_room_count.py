"""Run: uv run --group dev python -m pytest tests

The room caps used to be enforced by walking every bucket on every new room — ~16 ms a
create at the live caps — and that walk ran under a service-wide flock that also spanned the
append, its fsync and any compaction. Room and note creation therefore had a global
concurrency of one across every worker (#578).

`.usage` carries the room count as well as the byte total now, so the walk moved onto the
reaper and the create path reads a file. Three things have to hold, and the last two are the
ones that would actually hurt if they broke: creates of distinct rooms must really run at the
same time, the count must not drift *low* (a low count admits a room the cap should refuse),
and the reaper must not delete a directory out from under a create that is entering it.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _scandir_calls(monkeypatch, work) -> int:
    """How many directories `work` reads — the unit the old create path grew with."""
    import store

    calls = 0
    real = os.scandir

    def counting(path):
        nonlocal calls
        calls += 1
        return real(path)

    monkeypatch.setattr(store.os, "scandir", counting)
    work()
    monkeypatch.setattr(store.os, "scandir", real)
    return calls


def _settled(root: Path, rooms: int = 1) -> None:
    """A store with `rooms` rooms and a reap pass behind it, so `.usage` is established and
    the throttle keeps another pass from firing inside whatever the test is measuring."""
    import store

    for i in range(rooms):
        store._write_record(root, f"seed{i}", "bot", "hi")
    (root / ".reaped").unlink(missing_ok=True)
    store._reap(root)


# --------------------------------------------------------------------------- the cost


@pytest.mark.parametrize("rooms", [2, 40])
def test_a_new_room_reads_no_directories_at_any_store_size(tmp_path, monkeypatch, rooms):
    """Parametrised rather than looped so a failure names the size it failed at. The count
    must be identical for both — and it must be zero, which is the whole of #578 on this
    path: the check was one `scandir` per bucket plus a `stat` per room, so it grew with the
    store, and it was the thing the global create gate was held across.
    """
    import store

    root = tmp_path / f"store{rooms}"
    _settled(root, rooms)

    fresh = store.room_path(root, "brand-new")
    reads = _scandir_calls(monkeypatch, lambda: store._check_room_capacity(root, fresh))
    assert reads == 0, f"a new room read {reads} directories; both caps must come off .usage"


def test_creates_of_distinct_rooms_run_at_the_same_time(tmp_path, monkeypatch):
    """The regression test for #578 itself, stated as a topology rather than a duration.

    Four creates of four different names are made to meet at a barrier *inside* the write,
    after the cap check and the reservation. Under the old gate — one flock held from the
    capacity walk to the last fsync — only one of them could ever be in there, so the barrier
    could not fill and this fails by timeout rather than by a flaky millisecond count.
    """
    import store

    root = tmp_path
    _settled(root)
    parties = 4
    together = threading.Barrier(parties)
    real_last_seq = store.last_seq

    def wait_inside_the_write(r, room):
        if room.startswith("para"):
            together.wait(timeout=10)  # every party must be inside the body at once
        return real_last_seq(r, room)

    monkeypatch.setattr(store, "last_seq", wait_inside_the_write)
    failed = []

    def create(i):
        try:
            store._write_record(root, f"para{i}", "bot", "hi")
        except BaseException as exc:  # noqa: BLE001 - reported through `failed`, not swallowed
            failed.append(exc)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(parties)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]

    assert not failed, f"creates of distinct rooms could not overlap: {failed}"
    assert not [t for t in threads if t.is_alive()], "a create never finished"
    assert store._count_rooms(root)[0] == parties + 1, "and every one of them was written"


# --------------------------------------------------------------------------- the count


def test_racers_on_one_room_count_one_room(tmp_path) -> None:
    """The count is a reservation, and a create that turns out to be an *append* must not
    take one. Eight racers, one name, one file — and the count has to say one, not eight.

    This is why `_create_gate` takes the room's own lock before the counter: whoever loses
    blocks there, and by the time it looks the file exists, so it reserves nothing.
    """
    import store

    _settled(tmp_path)
    before = store._note_totals(tmp_path, store._count_rooms, name=store.USAGE_FILE)[0]
    start = threading.Barrier(8)

    def create(i):
        start.wait()
        store._write_record(tmp_path, "same", "bot", f"hi{i}")

    threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]

    counted = store._note_totals(tmp_path, store._count_rooms, name=store.USAGE_FILE)[0]
    assert counted == before + 1, "eight writes to one room are one room"
    assert store._count_rooms(tmp_path)[0] == counted, "and the count must match the disk"


def test_the_room_cap_binds_under_concurrent_creates_of_distinct_names(tmp_path) -> None:
    """Racing the cap is the thing a per-file lock cannot survive: without a shared counter
    each racer counts `cap - 1` and they all write. The check and the reservation happen in
    one critical section, so the cap holds even though the *writes* no longer serialise."""
    import store

    _settled(tmp_path)
    room_cap = store._count_rooms(tmp_path)[0] + 3
    start = threading.Barrier(12)
    refused = []

    def create(i):
        start.wait()
        try:
            store._write_record(tmp_path, f"race{i}", "bot", "hi")
        except store.StoreError:
            refused.append(i)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "MAX_ROOMS", room_cap)
        threads = [threading.Thread(target=create, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]

    on_disk = store._count_rooms(tmp_path)[0]
    assert refused, "premise: the cap was reached, or this proves nothing"
    assert on_disk == room_cap, f"{on_disk} rooms exist against a cap of {room_cap}"


def test_a_reap_cannot_lose_a_room_create_that_has_counted_but_not_written(
    tmp_path, monkeypatch
) -> None:
    """A create writes its `+1` reservation and its room at two different moments, and the
    reaper rewrites the count from a walk that cannot see a room not yet on disk. A pass
    landing between the two would write the lower figure, and a low count admits a room the
    cap should refuse.

    The shared `.usage.create` span is what closes it: a create holds it from before its
    reservation until after its write, and the reaper takes it exclusively. Driven from
    inside the write, so the reap really does land in the window rather than being timed to
    it, and joined with a bound — once the reap is ordered behind the span it *cannot*
    finish until the create does, so an unconditional wait would hang instead of failing.
    """
    import store

    _settled(tmp_path)
    before = store._count_rooms(tmp_path)[0]
    monkeypatch.setattr(store, "REAP_EVERY", 0)  # every pass is due, including this one
    real_last_seq = store.last_seq
    reaper = []

    def race_a_reap_from_inside_the_write(r, room):
        if room == "late" and not reaper:
            reaper.append(threading.Thread(target=store._reap, args=(tmp_path,), daemon=True))
            reaper[0].start()
            reaper[0].join(1.0)  # unfixed it finishes here and clobbers; fixed it is blocked
        return real_last_seq(r, room)

    monkeypatch.setattr(store, "last_seq", race_a_reap_from_inside_the_write)
    store._write_record(tmp_path, "late", "bot", "hi")

    assert reaper, "premise: the reap ran inside the create's reservation window"
    reaper[0].join(30)  # outside the span, so the blocked pass can now finish its walk
    assert not reaper[0].is_alive(), "the reap never completed"
    assert store._count_rooms(tmp_path)[0] == before + 1, "premise: the room is on disk"
    counted = store._note_totals(tmp_path, store._count_rooms, name=store.USAGE_FILE)[0]
    assert counted == before + 1, "a reap must not drop a reservation in flight"


def test_a_reap_cannot_remove_a_bucket_a_room_create_is_entering(tmp_path, monkeypatch) -> None:
    """`_locked` makes the room's bucket one `mkdir` before it creates the sidecar lock
    inside it, and in that instant the bucket holds nothing `_prune` would refuse. A pass
    landing there removed the directory out from under a create that had just made it, and
    the create died on the `open` — ENOENT, or EINVAL on APFS.

    The gate used to hold `.rooms-create` across that gap and the reaper's prune took the
    same file. Both now use the shared span, which is the same guarantee without making
    every create wait for every other one.
    """
    import store

    _settled(tmp_path)
    monkeypatch.setattr(store, "REAP_EVERY", 0)
    real_mkdir = Path.mkdir
    reaper = []

    def race_a_reap_between_the_mkdir_and_the_open(self, *args, **kwargs):
        made = real_mkdir(self, *args, **kwargs)
        if self.parent.name == "rooms" and not reaper:  # a room bucket, just created
            reaper.append(threading.Thread(target=store._reap, args=(tmp_path,), daemon=True))
            reaper[0].start()
            reaper[0].join(1.0)
        return made

    monkeypatch.setattr(Path, "mkdir", race_a_reap_between_the_mkdir_and_the_open)
    store._write_record(tmp_path, "entering", "bot", "hi")  # must not raise
    monkeypatch.undo()

    assert reaper, "premise: a reap ran inside the mkdir-to-open gap"
    reaper[0].join(30)
    assert store.room_path(tmp_path, "entering").exists(), "the create lost its own bucket"


# --------------------------------------------------------------------------- the format


def test_a_usage_file_from_before_the_count_heals_by_walking(tmp_path) -> None:
    """`.usage` held a single integer before it carried a count. It has no second field, so
    it parses as untrusted and the caps rebuild from a walk — the old cost, never a wrong
    answer — and the next reap rewrites it in the two-integer format.

    `room_bytes_used` is the one reader that must NOT walk: it runs on the append path, which
    is what the file exists to keep cheap. It reads the legacy file as 0, which is the same
    fail-open a missing file gets, and the write it gates is a compaction — so the cost of
    being wrong for one reap interval is a full ring kept, not a ring thrown away.
    """
    import store

    _settled(tmp_path, rooms=2)
    walked = store._count_rooms(tmp_path)
    (tmp_path / store.USAGE_FILE).write_text(str(walked[1]))  # the pre-#578 format

    assert store._note_totals(tmp_path, store._count_rooms, name=store.USAGE_FILE) == walked
    assert store.room_bytes_used(tmp_path) == 0, "the hot path fails open, it never walks"

    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert (tmp_path / store.USAGE_FILE).read_text().split() == [str(n) for n in walked]
    assert store.room_bytes_used(tmp_path) == walked[1], "and it reads back without a walk"
