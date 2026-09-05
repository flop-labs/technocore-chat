"""Run: uv run --group dev python -m pytest tests

One level of 256-way directory sharding, and the lazy migration that gets a live store
into it.

The migration is the half worth testing hardest. A store that is already sharded is just a
store with deeper paths; a store part-way through the move is one where the same room can be
in two places, and the failure that costs data is silent — the history stays on disk under
the old name while reads and writes go to a new empty file beside it.
"""

from __future__ import annotations

import os
import time

import pytest


def _legacy_room(root, name: str, *lines: str):
    """A room as a pre-sharding build left it: flat in `rooms/`, sidecar lock beside it."""
    d = root / "rooms"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.jsonl"
    path.write_bytes(b"".join(line.encode() + b"\n" for line in lines))
    (d / f"{name}.jsonl.lock").touch()
    return path


def _legacy_note(root, ns: str, key: str, value: str):
    d = root / "notes" / ns
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{key}.txt"
    path.write_text(value, encoding="utf-8")
    (d / f"{key}.txt.lock").touch()
    return path


def _record(seq: int, text: str) -> str:
    import json

    return json.dumps({"seq": seq, "ts": "2026-01-01T00:00:00Z", "from": "old", "text": text})


# --------------------------------------------------------------------------- the layout


def test_a_name_lands_in_the_bucket_its_hash_names(tmp_path):
    """The path is computed from the name and nothing else, so any process resolves it the
    same way with no index to consult and none to keep in sync.

    Spelled out here rather than by calling `_shard`, because this is an ON-DISK FORMAT: a
    test that derived the expectation from the implementation would agree with any change to
    it, including one that silently puts every existing file in the wrong bucket.
    """
    import hashlib

    import store

    store.append(tmp_path, "lobby", "alice", "hi")
    expected = tmp_path / "rooms" / hashlib.blake2b(b"lobby", digest_size=1).hexdigest()
    assert store.room_path(tmp_path, "lobby") == expected / "lobby.jsonl"
    assert (expected / "lobby.jsonl").exists(), "on disk where the hash says, not beside it"

    store.note_set(tmp_path, "did-a1", "z6mk", "v")
    key_digest = hashlib.blake2b(b"z6mk", digest_size=1).hexdigest()
    assert store.note_path(tmp_path, "did-a1", "z6mk") == (
        tmp_path / "notes" / "did-a1" / key_digest / "z6mk.txt"
    )
    assert len(key_digest) == 2, "one level of 256: two hex characters, never four"


def test_every_name_lands_in_one_of_256_buckets(tmp_path):
    """digest_size=1 is 8 bits, which is exactly 256 — so the width needs no mask and no
    slice, and there is no room for a modulo bias to hide in."""
    import store

    seen = {store._shard(f"name-{i:05d}") for i in range(20_000)}
    assert len(seen) == 256, f"{len(seen)} buckets reachable, expected all 256"
    assert all(len(b) == 2 and all(c in "0123456789abcdef" for c in b) for b in seen)


def test_the_bucket_does_not_move_between_processes(tmp_path):
    """`hash()` is salted per process by PYTHONHASHSEED, so a store that used it would put a
    room somewhere new after every restart. This is the regression test for reaching for it."""
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    code = f"import sys; sys.path.insert(0, {src!r}); import store; print(store._shard('lobby'))"
    seen = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert len(seen) == 1, f"the bucket moved with PYTHONHASHSEED: {seen}"


def test_the_key_parameter_is_a_hook_and_not_a_default(tmp_path):
    """`key` is reserved for a deployment that ever wants per-instance buckets. Unkeyed is
    what ships, so the two must not silently be the same function."""
    import store

    assert store._shard("lobby") == store._shard("lobby", None)
    assert store._shard("lobby") != store._shard("lobby", b"pepper")


# --------------------------------------------------------------------------- migration


def test_a_flat_room_moves_to_its_bucket_on_first_touch(tmp_path):
    """The dual-read half: nothing in the deploy moves these files, so the first read or
    write of each one does it."""
    import store

    legacy = _legacy_room(tmp_path, "old", _record(1, "one"), _record(2, "two"))
    view = store.read_messages(tmp_path, "old", limit=50)

    assert [m["text"] for m in view["messages"]] == ["one", "two"], "the history came back"
    assert not legacy.exists(), "…and the file is no longer where it was"
    assert store.room_path(tmp_path, "old").exists(), "…it is in its bucket"


def test_an_append_to_a_flat_room_keeps_its_history_rather_than_forking_it(tmp_path):
    """The bug the resolver exists to make unrepresentable.

    Migrate on read but write straight to the bucket and a live room ends up in two files:
    the old one holding every message, the new one holding the next one at `seq` 1. Reads
    check the bucket first, so the room silently loses its whole history and its sequence
    restarts — no error, no 500, nothing in a log. So the resolver returns ONE path per name
    per instant, the same one to readers and writers, and moves the file to match it — which
    means the two can never be looking at different files.
    """
    import store

    _legacy_room(tmp_path, "live", _record(1, "one"), _record(2, "two"))
    rec = store.append(tmp_path, "live", "alice", "three")

    assert rec["seq"] == 3, "the sequence continued rather than restarting"
    view = store.read_messages(tmp_path, "live", limit=50)
    assert [m["text"] for m in view["messages"]] == ["one", "two", "three"]
    on_disk = sorted(p.name for p in (tmp_path / "rooms").rglob("live.jsonl"))
    assert on_disk == ["live.jsonl"], f"the room exists in {len(on_disk)} places, not one"


def test_the_migration_never_replaces_a_sidecar_lock(tmp_path):
    """Migration moves the data and leaves the lock where it is.

    Moving the lock too is a lock-domain bug, and the shape of it is check-then-replace: a
    worker that already sees the migrated data can create and flock the destination between
    the "is it free?" test and the replace, and the replace then unlinks the inode it holds.
    Two workers then hold what both believe is the room lock, which is what `seq`, the nonce
    check and CAS are all serialised by.

    Asserted on the inode rather than on existence, because that is the failure: a lock that
    a live writer holds must never stop being the lock at its own path.
    """
    import store

    legacy = _legacy_room(tmp_path, "old", _record(1, "one"))
    legacy_lock = legacy.with_suffix(".jsonl.lock")

    store.append(tmp_path, "old", "alice", "two")
    fresh = store.room_path(tmp_path, "old").with_suffix(".jsonl.lock")

    assert legacy_lock.exists(), "the old sidecar is left for the orphan sweeper"
    assert fresh.exists(), "…and the moved data got its own, freshly created"
    assert fresh.stat().st_ino != legacy_lock.stat().st_ino, "no inode was replaced"


def test_the_sweeper_reclaims_the_lock_the_migration_left(tmp_path, monkeypatch):
    """Which is what makes leaving it free rather than a leak: the orphan sweep already
    exists for a lock whose data file is gone, and a migrated-away file is exactly that."""
    import store

    legacy = _legacy_room(tmp_path, "old", _record(1, "one"))
    legacy_lock = legacy.with_suffix(".jsonl.lock")
    store.append(tmp_path, "old", "alice", "two")
    assert legacy_lock.exists(), "premise: the migration left it"

    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(legacy_lock, (old, old))
    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)

    assert not legacy_lock.exists(), "an idle lock with no data file must be swept"
    assert store.read_messages(tmp_path, "old", limit=5)["messages"][-1]["text"] == "two"


def test_a_migration_that_cannot_write_still_serves_the_data(tmp_path, monkeypatch):
    """A read-only volume, or a restore whose ownership was never fixed, must not turn every
    unmigrated room into an empty one.

    An absent file is how this store spells "no such room", so a resolver that returned the
    bucket after a failed move would report the whole store as gone — silently, with the data
    plainly still on disk. Falling back is not the fork this design is shaped around: readers
    and writers still agree, because the fallback is taken only while the legacy file is the
    only copy that exists.
    """
    import store

    _legacy_room(tmp_path, "stuck", _record(1, "one"), _record(2, "two"))
    _legacy_note(tmp_path, "kv", "held", "value")

    def refuse(*args, **kwargs):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(store.os, "replace", refuse)

    view = store.read_messages(tmp_path, "stuck", limit=50)
    assert [m["text"] for m in view["messages"]] == ["one", "two"], "the history is served"
    assert store.room_path(tmp_path, "stuck") == tmp_path / "rooms" / "stuck.jsonl"
    assert store.note_get(tmp_path, "kv", "held") == "value"
    assert store.list_rooms(tmp_path) == ["stuck"], "…and it still lists"


def test_a_flat_note_moves_and_keeps_its_value(tmp_path):
    import store

    legacy = _legacy_note(tmp_path, "did-a1", "z6mk", "did:key:zabc")
    assert store.note_get(tmp_path, "did-a1", "z6mk") == "did:key:zabc"
    assert not legacy.exists()
    assert store.note_path(tmp_path, "did-a1", "z6mk").read_text() == "did:key:zabc"


def test_cas_sees_the_migrated_value_not_an_empty_bucket(tmp_path):
    """`if=` compares against what the note holds. A resolver that pointed CAS at the empty
    bucket while the value was still flat would turn every conditional write on an
    unmigrated note into a spurious 409 — and `if_absent` into a spurious success."""
    import store

    _legacy_note(tmp_path, "kv", "counter", "7")

    with pytest.raises(store.StoreConflictError):
        store.note_set(tmp_path, "kv", "counter", "8", expect="wrong")
    with pytest.raises(store.StoreConflictError):
        store.note_set(tmp_path, "kv", "counter", "8", expect_absent=True)

    store.note_set(tmp_path, "kv", "counter", "8", expect="7")
    assert store.note_get(tmp_path, "kv", "counter") == "8"


def test_a_half_migrated_store_lists_every_room_exactly_once(tmp_path):
    """Both depths are occupied for as long as the migration takes, so every walk has to be
    depth-agnostic — and must not double-count a name it meets at one depth."""
    import store

    _legacy_room(tmp_path, "flat-one", _record(1, "a"))
    _legacy_room(tmp_path, "flat-two", _record(1, "b"))
    store.append(tmp_path, "bucketed", "alice", "c")  # written sharded from birth

    assert store.list_rooms(tmp_path) == ["bucketed", "events", "flat-one", "flat-two"]
    assert store.room_stats(tmp_path)["total"] == 4
    assert store.service_stats(tmp_path)["rooms"]["total"] == 4


def test_racing_resolvers_migrate_a_room_once(tmp_path):
    """Two resolvers can both see the flat file. The first `os.replace` wins and the second
    fails on a source that is already gone, and both return the same bucketed path."""
    import threading

    import store

    _legacy_room(tmp_path, "hot", _record(1, "one"))
    start = threading.Barrier(8)
    seen, errors = [], []

    def resolve():
        start.wait()
        try:
            seen.append(store.room_path(tmp_path, "hot"))
        except BaseException as exc:  # noqa: BLE001 — recorded for the assertion, not hidden
            errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert not errors, f"a resolver raised: {errors}"
    assert len(set(seen)) == 1, "eight resolvers disagreed about where the room lives"
    assert seen[0].read_bytes().count(b"\n") == 1, "the history survived the race"


# ------------------------------------------------------------- caps, reaping, litter


def test_the_room_cap_counts_the_whole_tree_and_not_one_bucket(tmp_path, monkeypatch):
    """`_check_room_capacity` used to scan the room's own parent, which was `rooms/`. Under
    sharding the parent is the room's bucket, which holds about one room — so a cap read off
    it would never bind, and MAX_ROOMS would stop existing on a world-writable service."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 4)
    for i in range(3):
        store.append(tmp_path, f"room{i}", "bot", "hi")  # `events` is the fourth, announced
    with pytest.raises(store.StoreError, match="room limit"):
        store.append(tmp_path, "one-too-many", "bot", "hi")


def test_the_per_namespace_cap_counts_every_bucket_in_the_namespace(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 3)
    for i in range(3):
        store.note_set(tmp_path, "did-a1", f"k{i}", "v")
    with pytest.raises(store.StoreError, match="note limit"):
        store.note_set(tmp_path, "did-a1", "one-too-many", "v")


def test_the_namespace_count_file_stays_at_the_namespace(tmp_path):
    """`.notes-count` is read by everything that asks what a namespace holds. Written into a
    key's bucket instead it would be invisible to every one of them, and a per-namespace cap
    would rebuild by walking on every single create."""
    import store

    store.note_set(tmp_path, "did-a1", "z6mk", "v")
    assert (tmp_path / "notes" / "did-a1" / store.NOTES_FILE).exists()


def test_the_reaper_reaches_rooms_and_notes_inside_their_buckets(tmp_path, monkeypatch):
    """A reaper that stopped at the top of `rooms/` would find nothing to reap and the caps
    it backs would become permanent rather than idle-expiring."""
    import store

    store.append(tmp_path, "stale", "bot", "hi")
    store.note_set(tmp_path, "kv", "stale", "v")
    old = time.time() - store.IDLE_SECONDS - 60
    for path in list((tmp_path / "rooms").rglob("*.jsonl")) + list(
        (tmp_path / "notes").rglob("*.txt")
    ):
        os.utime(path, (old, old))

    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)

    assert list((tmp_path / "rooms").rglob("*.jsonl")) == []
    assert list((tmp_path / "notes").rglob("*.txt")) == []


def test_a_reaped_room_does_not_leave_its_bucket_behind(tmp_path, monkeypatch):
    """Emptied buckets are litter that never goes away on its own, and every later walk pays
    to open them. Unpruned they are also what stops an emptied namespace being dropped at
    all, since a directory of empty directories is not an empty directory."""
    import store

    store.append(tmp_path, "stale", "bot", "hi")
    store.note_set(tmp_path, "doomed", "k", "v")
    old = time.time() - store.IDLE_SECONDS - 60
    for path in (tmp_path / "rooms").rglob("*"):
        os.utime(path, (old, old))
    for path in (tmp_path / "notes").rglob("*"):
        os.utime(path, (old, old))

    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)
    store._reap(tmp_path)  # the sweep frees the locks; the pass after it frees the buckets

    assert not (tmp_path / "notes" / "doomed").exists(), "the drained namespace was dropped"
    leftover = [p for p in (tmp_path / "rooms").rglob("*") if p.is_dir()]
    assert leftover == [], f"empty buckets left behind: {leftover}"


def test_prune_survives_an_rmdir_race_on_a_bucket_refilled_under_it(tmp_path, monkeypatch):
    """The empty check and the rmdir are two syscalls, not one: a bucket that fills between
    them must not be forced empty by a caller that still thinks it is.

    `os.rmdir` refuses a non-empty directory with `OSError` on every platform this runs on,
    so a write landing in the gap between `_prune`'s own scan and its `rmdir` call reaches
    exactly the two lines this exercises. Faking the error is the deterministic half of
    that: it does not depend on winning a real race to land there, only on the branch
    existing — and it was previously unreached by the suite (see the coverage report).
    """
    import store

    bucket = tmp_path / "rooms" / "ab"
    bucket.mkdir(parents=True)
    real_rmdir = os.rmdir

    def flaky_rmdir(path, *a, **kw):
        if str(path) == str(bucket):
            raise OSError("simulated: refilled under us")
        return real_rmdir(path, *a, **kw)

    monkeypatch.setattr(os, "rmdir", flaky_rmdir)

    assert store._prune(tmp_path / "rooms") is False, (
        "a bucket whose rmdir was refused must not read as empty"
    )
    assert bucket.exists(), "a refused rmdir must leave the directory in place, not raise"


def test_a_reap_that_prunes_buckets_never_meets_a_create_gate_it_already_holds(
    tmp_path, monkeypatch
):
    """`_prune(rooms)` takes the room create span, and `_reap` runs on the write path — so
    the question is whether any caller can be inside that span when a pass fires.

    `flock` is per open file description and `_locked` opens a fresh fd every time, so a
    second acquire from the same thread does not re-enter: it blocks on itself, forever, with
    no EDEADLK and no traceback. A hung worker thread is the one failure a passing test suite
    would never show, because the ordinary suite touches `.reaped` and the throttle makes the
    second pass in a request a no-op.

    So: every pass is due, the marker never survives, and the writes that create rooms are
    driven under a watchdog. `append` also creates `events` and announces into it, which is
    the nested `_write_record` the reasoning has to be right about.
    """
    import threading

    import store

    monkeypatch.setattr(store, "REAP_EVERY", 0)
    done = []

    def writes():
        for i in range(4):
            (tmp_path / ".reaped").unlink(missing_ok=True)  # no throttle to hide the nesting
            store.append(tmp_path, f"room{i}", "bot", "hi")
            (tmp_path / ".reaped").unlink(missing_ok=True)
            store.note_set(tmp_path, f"ns{i}", "k", "v")
        done.append(True)

    worker = threading.Thread(target=writes, daemon=True)
    worker.start()
    worker.join(30)
    assert done, "a write deadlocked on a gate the reap it triggered was already holding"


def test_pruning_keeps_the_bucket_of_a_room_that_survived(tmp_path, monkeypatch):
    """The other half of the pruning claim, and the one the all-rooms-reaped case cannot
    make: a pass that removed occupied buckets as happily as empty ones would pass that test
    and lose every live room here."""
    import store

    store.append(tmp_path, "keeper", "bot", "hi")
    store.append(tmp_path, "goner", "bot", "hi")
    kept_bucket = store.room_path(tmp_path, "keeper").parent
    doomed_bucket = store.room_path(tmp_path, "goner").parent
    assert kept_bucket != doomed_bucket, "premise: the two rooms are in different buckets"

    old = time.time() - store.IDLE_SECONDS - 60
    for path in doomed_bucket.iterdir():
        os.utime(path, (old, old))

    monkeypatch.setattr(store, "REAP_EVERY", 0)
    store._reap(tmp_path)
    store._reap(tmp_path)  # the sweep frees the lock; the pass after it frees the bucket

    assert not doomed_bucket.exists(), "the emptied bucket was not pruned"
    assert store.room_path(tmp_path, "keeper").exists(), "…and the live room went with it"
    assert store.read_messages(tmp_path, "keeper", limit=5)["messages"][0]["text"] == "hi"
