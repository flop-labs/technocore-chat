"""Run: uv run --group dev python -m pytest tests

Sequence-level regression coverage for the rooms index (PR #633 review thread). Each
test pins one reviewer finding, in the interleaving they named:

- yukkie3276 #1: a malformed/truncated index line must fail CLOSED to the walk, not
  silently shrink the authoritative set.
- yukkie3276 #2 / Minh3132: an append landing between compaction's walk and its
  publish must survive the replace (the carry step), and an append landing after the
  publish must open the new inode.
- luch91: a reaped room must never appear in the listing and must not leave the
  page short; a failed index append must not leave a stale-but-authoritative index
  behind. Totals are the index's reap-time view (a stat per indexed room to make
  them exact is the O(total rooms) walk #576 removes), so they reconcile when the
  pass compacts — that is the contract these tests pin, not per-request exactness.
- WIZARDspace's trap: rooms are counted via append() (not _write_record, which never
  touches the index — a fixture built with it would silently exercise the walk
  fallback and prove nothing about this code), and the reaper throttle is armed
  explicitly where a pass is expected.
"""

import os
import time

import orjson

import roomsindex
import store  # src/ is on sys.path via pyproject's pytest pythonpath

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _index_path(root):
    return root / roomsindex.ROOMS_INDEX_FILE


def _write_index(root, *entries):
    """Hand-craft an index file: entries are dicts (serialized) or raw byte lines."""
    blob = b""
    for e in entries:
        if isinstance(e, bytes):
            blob += e
        else:
            blob += (
                orjson.dumps({"room": e["room"], "mtime": e["mtime"], "size": e["size"]}) + b"\n"
            )
    _index_path(root).write_bytes(blob)


def _read_index(root):
    return roomsindex.read(root)


def _seed(root, n, prefix="r"):
    """n rooms via append(), so every one lands in the index (WIZARDspace's trap).

    Note the events-room side effect: append() announces the first created room via
    _log_event, so the store holds n + 1 rooms on disk (the n seeded plus events).
    Callers below account for that +1.
    """
    for i in range(n):
        store.append(root, f"{prefix}{i}", "bot", "hi")


def _age_files(root, names, days=30):
    """Pull mtimes back, the lever the reap tests use to make rooms reapable."""
    old = time.time() - days * 86400
    for name in names:
        os.utime(store.room_path(root, name), (old, old))


def _unlistable_room(root, name):
    """Put a room on disk that /rooms must never enumerate, bypassing every writer.

    Written straight to the resolved path because the API refuses to create one: these
    exist on a real volume (an older validator, a restore, an operator copying a tree in)
    and the filter has to hold for files nobody in this codebase wrote.
    """
    path = store.room_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}\n")
    return path


class _VanishedEntry:
    """A scandir entry that no longer exists by the time anyone stats it.

    The reaper deletes a file after readdir has already produced the entry, so this is
    an ordinary interleaving rather than a fault — the tests below use it to reach the
    `continue` that keeps one dead room from failing a whole pass or a whole page.
    """

    def __init__(self, name):
        self.name = name

    def stat(self):
        raise FileNotFoundError(2, "No such file or directory", self.name)


# --------------------------------------------------------------------------
# Fail-closed parsing (yukkie3276 #1)
# --------------------------------------------------------------------------


class TestFailClosedParsing:
    def test_truncated_line_sends_whole_read_to_walk(self, tmp_path):
        """The reviewer's minimal repro: two real rooms, an index holding one valid
        line and one truncated line. The read must return {} — NOT a one-room dict
        that would then be treated as complete and hide the second room."""
        _seed(tmp_path, 2, prefix="x")
        _write_index(
            tmp_path,
            {"room": "x0", "mtime": 123.0, "size": 64},
            b'{"room":"x1","mtime":',
        )
        assert _read_index(tmp_path) == {}

    def test_corrupt_index_full_stats_falls_back_to_walk(self, tmp_path):
        """room_stats with a corrupt index answers from the walk: every room present,
        exact total — never a set that merely matches the surviving lines."""
        _seed(tmp_path, 4)
        _write_index(
            tmp_path,
            {"room": "r0", "mtime": 1.0, "size": 64},
            b'{"room":"r1","mt',
        )
        stats = store.room_stats(tmp_path, limit=10)
        # 4 seeded + events, all from the walk: the corrupt index contributes nothing.
        assert stats["total"] == 5
        assert {r["room"] for r in stats["rooms"]} == {
            "r0",
            "r1",
            "r2",
            "r3",
            store.EVENTS_ROOM,
        }

    def test_blank_lines_ok_garbage_line_poisons(self, tmp_path):
        _seed(tmp_path, 1)
        _write_index(tmp_path, b"\n", {"room": "r0", "mtime": 5.0, "size": 20}, b"\n")
        assert _read_index(tmp_path) == {"r0": (5.0, 20)}
        _write_index(tmp_path, {"room": "r0", "mtime": 5.0, "size": 20}, b"garbage\n")
        assert _read_index(tmp_path) == {}

    def test_wrong_field_types_poison(self, tmp_path):
        _seed(tmp_path, 1)
        _write_index(tmp_path, {"room": "r0", "mtime": "not-a-float", "size": 20})
        assert _read_index(tmp_path) == {}
        _write_index(tmp_path, {"room": "r0", "mtime": 5.0, "size": -1})
        assert _read_index(tmp_path) == {}

    def test_unlistable_names_are_skipped_not_poison(self, tmp_path):
        _seed(tmp_path, 1)
        _write_index(
            tmp_path,
            {"room": "p-secret-capability", "mtime": 1.0, "size": 5},
            {"room": "r0", "mtime": 2.0, "size": 20},
        )
        assert _read_index(tmp_path) == {"r0": (2.0, 20)}


# --------------------------------------------------------------------------
# Append vs compaction (yukkie3276 #2, Minh3132)
# --------------------------------------------------------------------------


class TestAppendVsCompaction:
    def test_append_between_walk_and_publish_is_carried(self, tmp_path, monkeypatch):
        """THE regression both reviewers asked for. The publisher collects a snapshot;
        an append lands after that walk but before the publish (hooked on `collect`,
        which sits exactly between the snapshot and the replace); the replace must not
        drop that line — the carry step re-reads the pathname under the exclusive index
        lock and carries it in."""
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)  # the index must exist first, or observe() writes nothing

        real_collect = roomsindex.collect

        def collect_then_append(root):
            # A REAL append landing between the snapshot and the publish: the room file
            # is written and its index line appended, exactly the interleaving
            # yukkie3276/Minh3132 described. Restore the hook first so the nested
            # append's own lifecycle cannot recurse back into it.
            monkeypatch.setattr(roomsindex, "collect", real_collect)
            snapshot = real_collect(root)
            store.append(tmp_path, "late", "bot", "mid-compaction create")
            return snapshot

        monkeypatch.setattr(roomsindex, "collect", collect_then_append)
        roomsindex.compact(tmp_path)

        assert "late" in _read_index(tmp_path)
        stats = store.room_stats(tmp_path, limit=10)
        # r0, r1, r2, the carried late room, + events.
        assert stats["total"] == 5
        assert {r["room"] for r in stats["rooms"]} == {
            "r0",
            "r1",
            "r2",
            "late",
            store.EVENTS_ROOM,
        }

    def test_append_after_publish_lands_on_new_inode(self, tmp_path):
        """The other half of the Minh3132 interleaving: an append that comes up after
        the replace has committed opens the NEW inode, and its line is readable."""
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)
        before = _index_path(tmp_path).read_bytes()
        store.append(tmp_path, "r1", "bot", "after publish")
        after = _index_path(tmp_path).read_bytes()
        assert after.count(b"\n") == before.count(b"\n") + 1  # appended, not rewritten
        assert "r1" in _read_index(tmp_path)
        assert store.room_stats(tmp_path, limit=10)["total"] == 4  # 3 + events

    def test_compaction_is_idempotent_and_complete(self, tmp_path):
        _seed(tmp_path, 5)
        store.append(tmp_path, "r2", "bot", "duplicate lines for r2 accumulate")
        roomsindex.compact(tmp_path)
        lines = [ln for ln in _index_path(tmp_path).read_bytes().split(b"\n") if ln.strip()]
        assert len(lines) == 6  # 5 rooms + events: exactly one line per room
        roomsindex.compact(tmp_path)
        assert set(_read_index(tmp_path)) == {f"r{i}" for i in range(5)} | {
            store.EVENTS_ROOM,
        }

    def test_lock_order_append_shared_compaction_exclusive(self, tmp_path):
        """The serialization boundary: appenders hold ROOMS_INDEX_LOCK shared (they
        must not block each other) and only the publisher holds it exclusive. Pin
        the flags, since the carry protocol's correctness rests on them."""
        import inspect

        src = inspect.getsource(roomsindex.observe)
        assert "_locked(path, shared=True)" in src
        csrc = inspect.getsource(roomsindex.compact)
        assert "_locked(path)" in csrc


# --------------------------------------------------------------------------
# Reaped rooms: totals, bytes, page fullness (luch91, WIZARDspace arms 1+2)
# --------------------------------------------------------------------------


class TestReapedRoomsReconciled:
    def test_reaped_rooms_leave_the_page_and_reconcile_at_compaction(self, tmp_path):
        """WIZARDspace's arm 1+2 under the reap-time totals contract. Compact, delete
        rooms the way a reap pass does (here without the compaction that normally follows
        it immediately), read WITHOUT compacting. The listing must never contain the dead
        and must not come back short — the freed ranks are backfilled, so limit=4 of 6
        live rooms returns 4. Totals stay the last-published (reap-time) view until the
        pass compacts; making them per-request exact would be one stat per indexed room,
        the O(total rooms) syscall walk #576 exists to remove.
        """
        _seed(tmp_path, 10)
        roomsindex.compact(tmp_path)
        dead = [f"r{i}" for i in range(5)]  # the 5 oldest
        for name in dead:
            store.room_path(tmp_path, name).unlink()

        # The page is fresh even before a compaction: dead rooms are skipped and the
        # page backfills, so a limit=4 request returns 4 rooms, none of them dead.
        stats = store.room_stats(tmp_path, limit=4)
        assert len(stats["rooms"]) == 4  # full page, not the short page the review saw
        assert not ({r["room"] for r in stats["rooms"]} & set(dead))
        # Totals are the index's last-published view: 10 seeded + events, until a pass
        # compacts. (`.usage` would also carry the dead, and counts p- rooms besides.)
        assert stats["total"] == 11

        # The compaction a reap pass runs right after its deletes reconciles both totals
        # and the listing to what is on disk.
        roomsindex.compact(tmp_path)
        stats = store.room_stats(tmp_path, limit=50)
        assert stats["total"] == 6
        expected_bytes = store.room_path(tmp_path, store.EVENTS_ROOM).stat().st_size
        for name in (f"r{i}" for i in range(5, 10)):
            expected_bytes += store.room_path(tmp_path, name).stat().st_size
        assert stats["bytes"] == expected_bytes

    def test_a_dead_entry_at_the_top_backfills_from_below(self, tmp_path):
        """A reaped room that is the *newest* in the index is skipped mid-scan and the next
        entry fills its rank — the page must still be `limit` long, not short by one."""
        _seed(tmp_path, 3)
        future = time.time() + 3600
        os.utime(store.room_path(tmp_path, "r2"), (future, future))  # r2 now sorts first
        roomsindex.compact(tmp_path)
        store.room_path(tmp_path, "r2").unlink()

        stats = store.room_stats(tmp_path, limit=3)
        assert len(stats["rooms"]) == 3  # backfilled, not the short page
        assert "r2" not in {r["room"] for r in stats["rooms"]}

    def test_index_claiming_a_page_it_cannot_fill_falls_back_to_the_walk(self, tmp_path):
        """If the index claims at least a page but the rooms behind it are gone, a page
        cannot be filled — the index is dead and the walk is the only authority left.
        """
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)  # index: r0, r1, r2, events
        for name in ("r0", "r1", "r2"):
            store.room_path(tmp_path, name).unlink()  # only events survives

        stats = store.room_stats(tmp_path, limit=4)
        assert stats["total"] == 1  # from the walk, not the dead index's 4
        assert [r["room"] for r in stats["rooms"]] == [store.EVENTS_ROOM]

    def test_full_reap_pass_leaves_index_equal_to_disk(self, tmp_path):
        """The real lifecycle end-to-end: reap for real (throttle armed), which deletes
        AND compacts, then the index describes the disk exactly and /rooms agrees."""
        _seed(tmp_path, 6)
        _age_files(tmp_path, [f"r{i}" for i in range(4)])  # 4 ancient, 2 fresh
        (tmp_path / ".reaped").unlink(missing_ok=True)
        store._reap(tmp_path)

        on_disk = {e.name[: -len(".jsonl")] for e in store._walk(tmp_path / "rooms", ".jsonl")}
        assert on_disk == {"r4", "r5", store.EVENTS_ROOM}
        assert set(_read_index(tmp_path)) == on_disk
        stats = store.room_stats(tmp_path, limit=50)
        assert stats["total"] == 3
        assert {r["room"] for r in stats["rooms"]} == on_disk

    def test_reap_compacts_from_its_own_walk_not_a_second_one(self, tmp_path, monkeypatch):
        """The tail compaction rides the pass's OWN walk (the `st` it already took), so a
        reap never costs two passes over the tree — the claim the comment in _reap_pass
        makes, pinned so a future edit cannot quietly reintroduce the second walk."""
        _seed(tmp_path, 5)
        _age_files(tmp_path, ["r0", "r1"])
        (tmp_path / ".reaped").unlink(missing_ok=True)

        taken = []
        monkeypatch.setattr(roomsindex, "collect", lambda root: taken.append(root) or {})
        store._reap(tmp_path)
        assert taken == [], "the reaper must not take a second walk to build the index"

        on_disk = {e.name[: -len(".jsonl")] for e in store._walk(tmp_path / "rooms", ".jsonl")}
        assert set(_read_index(tmp_path)) == on_disk  # still complete, from the one walk

    def test_a_reap_survives_an_unwritable_index(self, tmp_path):
        """A failed tail compaction must not turn a finished reap into a raised one; the
        reader then fails closed to the walk, so nothing is served from a stale file."""
        _seed(tmp_path, 3)
        _index_path(tmp_path).unlink(missing_ok=True)
        _index_path(tmp_path).mkdir()  # os.replace onto a directory raises OSError
        (tmp_path / ".reaped").unlink(missing_ok=True)

        store._reap(tmp_path)  # must not raise

        stats = store.room_stats(tmp_path, limit=10)
        assert stats["total"] == 4  # 3 seeded + events, from the walk
        assert {r["room"] for r in stats["rooms"]} == {"r0", "r1", "r2", store.EVENTS_ROOM}


# --------------------------------------------------------------------------
# Failed append quarantines a stale authoritative index (luch91)
# --------------------------------------------------------------------------


class TestFailedAppendQuarantines:
    def test_failed_append_does_not_hide_the_new_room(self, tmp_path):
        """luch91's second case: the index append fails while a stale index exists.
        Before the fix the new room was silently absent from /rooms; now the failed
        append quarantines the index, so the reader fails closed to the walk — where
        the new room is the newest thing on disk and must rank first."""
        _seed(tmp_path, 5)
        roomsindex.compact(tmp_path)
        # Make the next index append fail deterministically (works even as root):
        # the data path is a directory, so open("ab") raises IsADirectoryError.
        _index_path(tmp_path).unlink()
        _index_path(tmp_path).mkdir()

        store.append(tmp_path, "brand-new", "bot", "must be discoverable")

        assert not _index_path(tmp_path).exists()  # quarantined, not trusted stale
        # /rooms walks: brand-new must be discoverable in the recency listing (it
        # was entirely absent before the fix). events may outrank it — the create
        # announcement touches the events file after the room record lands.
        stats = store.room_stats(tmp_path, limit=3)
        assert "brand-new" in {r["room"] for r in stats["rooms"]}
        assert stats["total"] == 7  # 5 seeded + events + brand-new

    def test_corrupt_quarantine_leftover_is_ignored(self, tmp_path):
        """A pre-existing .corrupt file must not block a later quarantine rename, and
        must never be read as the index."""
        _seed(tmp_path, 3)
        (tmp_path / (roomsindex.ROOMS_INDEX_FILE + roomsindex.CORRUPT_SUFFIX)).write_bytes(b"junk")
        _index_path(tmp_path).unlink()
        _index_path(tmp_path).mkdir()
        store.append(tmp_path, "another", "bot", "hi")
        assert store.room_stats(tmp_path, limit=10)["total"] == 5  # 3 + events + another


# --------------------------------------------------------------------------
# Index population and ordering
# --------------------------------------------------------------------------


class TestPopulation:
    def test_events_room_indexed_immediately_on_first_create(self, tmp_path):
        """The EVENTS_ROOM bypass: _log_event writes it via _write_record, which
        never touches append(), so it must be indexed explicitly at first create."""
        _seed(tmp_path, 1)
        assert store.EVENTS_ROOM in _read_index(tmp_path)

    def test_compaction_seeds_from_full_walk_when_no_index(self, tmp_path):
        """The create-time seed path: compaction with no index builds a complete one
        from its own walk — no append-time inline walk needed."""
        _seed(tmp_path, 4)
        assert not _index_path(tmp_path).exists() or _read_index(tmp_path)
        _index_path(tmp_path).unlink(missing_ok=True)
        roomsindex.compact(tmp_path)
        assert set(_read_index(tmp_path)) == {f"r{i}" for i in range(4)} | {store.EVENTS_ROOM}

    def test_fresh_stat_wins_over_stale_index_mtime(self, tmp_path):
        """room_stats re-stats the page: a stale index mtime must not flip the order
        or the idle seconds the response reports."""
        _seed(tmp_path, 2)  # r0, r1, events on disk; only r0/r1 go in the hand-made index
        old = time.time() - 86400
        os.utime(store.room_path(tmp_path, "r0"), (old, old))  # r0 now oldest on disk
        _write_index(
            tmp_path,
            {"room": "r0", "mtime": time.time(), "size": 64},  # index claims r0 newest
            {"room": "r1", "mtime": old, "size": 64},
        )
        stats = store.room_stats(tmp_path, limit=2)
        assert stats["rooms"][0]["room"] == "r1"
        assert stats["rooms"][1]["room"] == "r0"
        assert stats["rooms"][0]["idle_seconds"] < stats["rooms"][1]["idle_seconds"]


# --------------------------------------------------------------------------
# One room dying mid-walk must not fail the pass or the page
# --------------------------------------------------------------------------


class TestRoomVanishesMidWalk:
    """The `continue` on a failed stat, in all three places that walk rooms.

    Same interleaving, three consumers: the reaper's snapshot builder, compaction's
    rebuild walk, and the walk `/rooms` falls back to. A room deleted between readdir
    and stat is not an error in any of them, and each has to keep going rather than
    raise out of a finished reap or hand back a short page.
    """

    def test_rebuild_walk_skips_a_room_deleted_before_its_stat(self, tmp_path, monkeypatch):
        _seed(tmp_path, 2)
        real_walk = store._walk

        def walk_with_a_ghost(d, suffix):
            for e in real_walk(d, suffix):
                if e.name.endswith("r1.jsonl"):
                    yield _VanishedEntry(e.name)
                else:
                    yield e

        monkeypatch.setattr(store, "_walk", walk_with_a_ghost)
        index = roomsindex.collect(tmp_path)
        # r1 is skipped, the other seeded room and events are collected.
        assert set(index) == {"r0", store.EVENTS_ROOM}

    def test_fallback_walk_skips_a_room_deleted_before_its_stat(self, tmp_path, monkeypatch):
        """listing()'s own walk: no index at all, and one entry dies under it."""
        _seed(tmp_path, 3)
        _index_path(tmp_path).unlink(missing_ok=True)  # force the walk, not the index
        assert _read_index(tmp_path) == {}
        real_walk = store._walk

        def walk_with_a_ghost(d, suffix):
            for e in real_walk(d, suffix):
                if e.name.endswith("r2.jsonl"):
                    yield _VanishedEntry(e.name)
                else:
                    yield e

        monkeypatch.setattr(store, "_walk", walk_with_a_ghost)
        page, total, total_bytes = roomsindex.listing(tmp_path, limit=10)
        names = {name for _, _, name, _ in page}
        assert "r2" not in names
        assert names == {"r0", "r1", store.EVENTS_ROOM}
        assert total == 3  # the ghost is counted by neither the page nor the total
        assert total_bytes == sum(size for _, size, _, _ in page)

    def test_reap_walk_survives_a_room_deleted_before_its_stat(self, tmp_path, monkeypatch):
        """End to end: the reaper's walk yields an entry that is already gone, and the
        pass must still finish — deleting what it can, publishing a complete snapshot."""
        _seed(tmp_path, 4)
        _age_files(tmp_path, ["r0", "r1"])
        (tmp_path / ".reaped").unlink(missing_ok=True)

        real_see = roomsindex.reap_see

        def see_but_stat_raises(root, entry, st):
            # r0 dies between the walk's readdir and the stat this call is handed. The
            # pass must skip it — no count, no snapshot entry, no delete — and carry on.
            if entry.name.endswith("r0.jsonl"):
                raise FileNotFoundError(2, "No such file or directory", entry.name)
            real_see(root, entry, st)

        monkeypatch.setattr(roomsindex, "reap_see", see_but_stat_raises)
        store._reap(tmp_path)  # must not raise

        # r0's file is still on disk (the pass never deleted it) and its old index line
        # is carried onto the publish, so the index still describes the disk exactly. The
        # property under test is that the pass FINISHED and stayed truthful, not that the
        # skipped room vanished — it is still there.
        on_disk = {e.name[: -len(".jsonl")] for e in store._walk(tmp_path / "rooms", ".jsonl")}
        assert "r0" in on_disk
        assert set(_read_index(tmp_path)) == on_disk
        stats = store.room_stats(tmp_path, limit=50)
        assert stats["total"] == len(on_disk)
        assert {r["room"] for r in stats["rooms"]} == on_disk


# --------------------------------------------------------------------------
# collect() and listing() agree on what a room is
# --------------------------------------------------------------------------


class TestUnlistableRoomsStayOut:
    def test_rebuild_walk_excludes_a_capability_room_on_disk(self, tmp_path):
        """`collect` is `_listable`-filtered for the same reason `listing`'s walk is: a
        `p-` room that exists on disk must not become an index entry, because an index
        entry is authoritative and would surface a capability URL in /rooms."""
        _seed(tmp_path, 2)
        _unlistable_room(tmp_path, "p-abcdef123456")
        index = roomsindex.collect(tmp_path)
        assert set(index) == {"r0", "r1", store.EVENTS_ROOM}
        roomsindex.compact(tmp_path)
        assert "p-abcdef123456" not in _read_index(tmp_path)
        stats = store.room_stats(tmp_path, limit=50)
        assert "p-abcdef123456" not in {r["room"] for r in stats["rooms"]}
        assert stats["total"] == 3  # 2 seeded + events; the p- room counts nowhere

    def test_fallback_walk_excludes_a_capability_room_on_disk(self, tmp_path):
        """Same filter on the walk, with no index in play — the two must not disagree."""
        _seed(tmp_path, 1)
        _unlistable_room(tmp_path, "mb-p-abcdef123456")
        _index_path(tmp_path).unlink(missing_ok=True)
        page, total, _ = roomsindex.listing(tmp_path, limit=50)
        assert {name for _, _, name, _ in page} == {"r0", store.EVENTS_ROOM}
        assert total == 2


# --------------------------------------------------------------------------
# observe() never builds a partial index
# --------------------------------------------------------------------------


class TestObserveNeverCreatesAPartialIndex:
    def test_vanished_index_is_not_recreated_from_one_line(self, tmp_path, monkeypatch):
        """The invariant this module states: the pathname exists only when it is complete.

        An append whose own `open("ab")` fails, and which then finds the file already
        gone (a concurrent appender quarantined it), must NOT write its single line into
        the gap. That file would read back as a complete one-room index on a store of
        thousands, and `listing` — which trusts a short index — would serve a page of one.
        Absent stays absent; the next reap pass seeds it from a full walk.
        """
        _seed(tmp_path, 5)
        roomsindex.compact(tmp_path)
        _index_path(tmp_path).unlink()  # quarantined by someone else mid-append

        # Force the shared-hold append down the OSError path with a file that is absent,
        # which is the state this branch exists to handle.
        def open_raises_oserror(path, mode="r", *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr("builtins.open", open_raises_oserror)
        roomsindex.observe(tmp_path, "ghost", 1.0, 5)

        assert not _index_path(tmp_path).exists()
        # Nothing partial to read, so /rooms walks and sees all six rooms.
        stats = store.room_stats(tmp_path, limit=50)
        assert stats["total"] == 6  # 5 seeded + events

    def test_a_failed_append_against_a_real_index_quarantines_it(self, tmp_path):
        """The other half: an index that exists and cannot be appended to is renamed
        aside, not left behind as a stale-but-authoritative snapshot."""
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)
        _index_path(tmp_path).unlink()
        _index_path(tmp_path).mkdir()  # open("ab") raises IsADirectoryError
        roomsindex.observe(tmp_path, "ghost", 1.0, 5)
        assert not _index_path(tmp_path).exists()
        assert (tmp_path / (roomsindex.ROOMS_INDEX_FILE + roomsindex.CORRUPT_SUFFIX)).is_dir()

    def test_index_vanishing_mid_append_is_survived_not_raised(self, tmp_path, monkeypatch):
        """A quarantiner removes the file between the exists-check and the append, so the
        append raises FileNotFoundError. That is a normal race with a compaction or a
        peer append, not a fault: observe must swallow it and leave the store walking
        rather than surfacing an error out of an append whose write already succeeded."""
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)
        real_open = open

        def open_raises_filenotfound(path, mode="r", *args, **kwargs):
            if str(path) == str(_index_path(tmp_path)):
                raise FileNotFoundError(2, "No such file or directory", str(path))
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", open_raises_filenotfound)
        roomsindex.observe(tmp_path, "ghost", 1.0, 5)  # must not raise
        assert _read_index(tmp_path)  # the old index is untouched and still readable

    def test_index_quarantined_by_a_peer_appender_is_left_absent(self, tmp_path, monkeypatch):
        """The append fails with a non-ENOENT OSError AND a peer has already quarantined
        the file, so the exclusive re-check finds nothing to rename. That must be a
        no-op: no line written, no file created, nothing raised."""
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)
        real_open = open

        def open_fails_after_the_peer_quarantines(path, mode="r", *args, **kwargs):
            if str(path) == str(_index_path(tmp_path)):
                _index_path(tmp_path).unlink()  # the peer's rename, landing right here
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", open_fails_after_the_peer_quarantines)
        roomsindex.observe(tmp_path, "ghost", 1.0, 5)  # must not raise
        assert not _index_path(tmp_path).exists()
        stats = store.room_stats(tmp_path, limit=50)
        assert stats["total"] == 4  # 3 seeded + events, from the walk


# --------------------------------------------------------------------------
# observe_file()'s own failure modes
# --------------------------------------------------------------------------


class TestObserveFileFailureModes:
    def test_missing_room_file_is_not_observed(self, tmp_path):
        """The write has succeeded but the file is already gone (reaped between the
        record landing and this stat): nothing to observe, and no error."""
        _seed(tmp_path, 2)
        roomsindex.compact(tmp_path)
        roomsindex.observe_file(tmp_path, "never-existed")  # must not raise
        assert "never-existed" not in _read_index(tmp_path)

    def test_unstattable_room_file_is_swallowed(self, tmp_path, monkeypatch):
        """A stat that raises OSError is the same best-effort contract as a missing file:
        observe_file is bookkeeping after a write that already succeeded, so it must not
        turn that success into a failure."""
        _seed(tmp_path, 2)
        roomsindex.compact(tmp_path)
        real_room_path = store.room_path

        def unstattable(root, room):
            path = real_room_path(root, room)
            if room == "r0":
                raise OSError(5, "I/O error", str(path))
            return path

        monkeypatch.setattr(store, "room_path", unstattable)
        roomsindex.observe_file(tmp_path, "r0")  # must not raise

    def test_observed_room_appears_in_the_index(self, tmp_path):
        """The happy path of the helper itself: a real room file is stat'd and observed."""
        _seed(tmp_path, 2)
        roomsindex.compact(tmp_path)
        roomsindex.observe_file(tmp_path, "r0")
        index = _read_index(tmp_path)
        st = store.room_path(tmp_path, "r0").stat()
        assert index["r0"] == (st.st_mtime, st.st_size)


# --------------------------------------------------------------------------
# compact()'s carry loop over a poisoned line
# --------------------------------------------------------------------------


class TestCompactCarrySurvivesPoison:
    def test_poisoned_line_does_not_abort_the_publish(self, tmp_path):
        """A line that starts like a record but does not parse must be skipped, not raise:
        the publish is the tail of a reap that has already deleted its files, and failing
        here would strand the index at its pre-reap contents — reporting rooms that no
        longer exist. The carried good lines still land."""
        _seed(tmp_path, 3)
        roomsindex.compact(tmp_path)
        # A line that starts with `{"room":` (so the cheap prefix check lets it through)
        # and then fails to parse: exactly the shape a torn write leaves behind.
        late = store.room_path(tmp_path, "late")
        late.parent.mkdir(parents=True, exist_ok=True)
        late.write_bytes(b"{}\n")  # on disk, so the carry step keeps its line
        with open(_index_path(tmp_path), "ab") as f:
            f.write(b'{"room": not json at all\n')
            f.write(b'{"room":"late","mtime":9e9,"size":7}\n')

        roomsindex.compact(tmp_path)  # must not raise

        index = _read_index(tmp_path)
        assert "late" in index  # the parseable carry survived the poisoned neighbour
        assert set(index) >= {"r0", "r1", "r2", store.EVENTS_ROOM}

    def test_poisoned_line_alone_still_publishes_the_snapshot(self, tmp_path):
        """With nothing parseable to carry, the snapshot alone is still complete and is
        still published — a reader must not be left holding the previous file."""
        _seed(tmp_path, 2)
        roomsindex.compact(tmp_path)
        with open(_index_path(tmp_path), "wb") as f:
            f.write(b'{"room":\n')
        roomsindex.compact(tmp_path)
        assert set(_read_index(tmp_path)) == {"r0", "r1", store.EVENTS_ROOM}
