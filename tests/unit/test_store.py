"""Run: uv run --group dev python -m pytest tests"""

import json
import os
import time
from contextlib import contextmanager

import pytest
from _client import (
    _age,
    _at,
    _keypair,
    _race_before_lock,
    _stats_for,
)


def _arm_reaper(root):
    """Clear the once-per-REAP_EVERY throttle, so the next write runs a pass."""
    (root / ".reaped").unlink(missing_ok=True)


def _reap_now(root):
    """Run a pass immediately, throttle and all."""
    import store

    _arm_reaper(root)
    store._reap(root)


def _race_under_lock(monkeypatch, store, action):
    """Run `action(target)` after the store takes a lock, before it acts on the file.

    The other half of the same idea, for the checks the store performs *under* the lock:
    a writer that lands here has beaten the recheck rather than the read.
    """
    real_locked = store._locked

    @contextmanager
    def hook(target):
        with real_locked(target):
            action(target)
            yield

    monkeypatch.setattr(store, "_locked", hook)


def test_compaction_bounds_file_and_keeps_seq(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 4096)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 2048)
    for _ in range(200):
        store.append(tmp_path, "big", "bot", "x" * 100)
    path = store.room_path(tmp_path, "big")
    assert path.stat().st_size <= 4096
    view = store.read_messages(tmp_path, "big", limit=50)
    assert view["last_seq"] == 200 and view["first_seq"] > 1  # gap is observable


def test_room_count_is_capped_so_disk_is_bounded(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 3)
    # The events room is a real room on disk and counts against the cap — it costs one
    # slot, once, on the first public room created.
    store.append(tmp_path, "room0", "bot", "hi")  # creates room0 AND events -> 2
    store.append(tmp_path, "room1", "bot", "hi")  # -> 3, at the cap
    store.append(tmp_path, "room1", "bot", "still fine")  # existing rooms keep working
    with pytest.raises(store.StoreError, match="room limit") as refused:
        store.append(tmp_path, "overflow", "bot", "hi")
    message = str(refused.value)
    assert "reuse one you already have" in message and "GET /rooms" in message
    assert "24 hours" in message and "7 days" in message


def test_room_disk_is_capped_independently_of_the_room_count(tmp_path, monkeypatch):
    """The bound that lets MAX_ROOMS grow without the volume growing.

    The room count used to *be* the disk budget (MAX_ROOMS * MAX_ROOM_BYTES). It no longer
    is, so the byte cap has to bite on its own — with the count cap nowhere near, which is
    exactly the case the old derivation could not express.
    """
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 10_000)  # far from binding: bytes must do it
    monkeypatch.setattr(store, "MAX_TOTAL_ROOM_BYTES", 400)
    store.append(tmp_path, "room0", "bot", "x" * 300)  # room0 + events ≈ 452B, over budget
    with pytest.raises(store.StoreError, match="room storage is full") as refused:
        store.append(tmp_path, "overflow", "bot", "hi")
    message = str(refused.value)
    assert "shorter name buys nothing" in message
    assert "reuse one you already have" in message and "GET /rooms" in message
    # The half that matters as much as the refusal: a room that exists is never cut off,
    # because compaction already holds it under MAX_ROOM_BYTES.
    store.append(tmp_path, "room0", "bot", "still fine")
    assert "still fine" in store.room_path(tmp_path, "room0").read_text()


def test_the_byte_budget_bounds_growth_and_not_only_creation(tmp_path, monkeypatch):
    """Rooms made while usage is low must not then grow past the budget.

    Gating creation alone left the documented bound false: create every room while the
    store is nearly empty, then fill each to its ring, and the total lands at
    MAX_ROOMS * MAX_ROOM_BYTES — ten times what the operator provisioned. Growing a room
    means appending to it, so the append is where the budget has to bite.
    """
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 8192)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 4096)
    monkeypatch.setattr(store, "MAX_TOTAL_ROOM_BYTES", 12_000)
    monkeypatch.setattr(store, "RESERVED_ROOM_BYTES", 2048)

    # Created early, while there is no pressure at all: both are allowed to exist.
    for room in ("first", "second"):
        store.append(tmp_path, room, "bot", "seed")

    def fill(room: str) -> int:
        for _ in range(40):
            store.append(tmp_path, room, "bot", "x" * 300)
        return store.room_path(tmp_path, room).stat().st_size

    # No pressure yet, so the full ring is available.
    assert fill("first") > store.RESERVED_ROOM_BYTES

    # Now make the budget look spent, as a reap pass would have recorded it, and keep
    # writing. The room that receives the writes yields back to its guaranteed floor.
    (tmp_path / store.USAGE_FILE).write_text(str(store.MAX_TOTAL_ROOM_BYTES + 1))
    assert fill("second") <= store.RESERVED_ROOM_BYTES
    assert fill("first") <= store.RESERVED_ROOM_BYTES, "an existing large room must yield too"

    # And the floor still holds a conversation rather than truncating to nothing.
    view = store.read_messages(tmp_path, "first", limit=5)
    assert view["messages"], "compaction must never empty a room"


def test_the_byte_budget_binds_at_the_cap_and_not_one_byte_past_it(tmp_path, monkeypatch):
    """Both budget comparisons are `at or over`, and both were only ever driven strictly
    over. `>=` vs `>` and `<` vs `<=` are invisible until usage lands exactly on the
    number, which is precisely where an operator who sized the disk expects it to bite."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 10_000)  # far from binding: bytes must do it
    store.append(tmp_path, "room0", "bot", "hi")
    used = store._scan(tmp_path / "rooms", ".jsonl", sized=True)[1]
    monkeypatch.setattr(store, "MAX_TOTAL_ROOM_BYTES", used)  # exactly at the budget

    with pytest.raises(store.StoreError, match="room storage is full"):
        store.append(tmp_path, "overflow", "bot", "hi")

    # The same equality on the growth half: at the budget a room gets its floor, not the
    # full ring, or "the budget bounds growth" is off by one byte.
    (tmp_path / store.USAGE_FILE).write_text(str(used))
    assert store._ring_limit(tmp_path) == store.RESERVED_ROOM_BYTES


def test_a_capacity_refusal_carries_the_numbers_a_caller_acts_on(tmp_path, monkeypatch):
    """These bodies are the service's answer to "now what", and the actionable part is the
    figures: the cap that was hit, and how full the disk is against how big it was sized.
    Matching only the opening words leaves every number in them free to be wrong."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 1)
    store.append(tmp_path, "only", "bot", "hi")
    with pytest.raises(store.StoreError, match=r"room limit reached \(1 is the cap"):
        store.append(tmp_path, "second", "bot", "hi")

    # Two note caps, two messages, and the number is the actionable part of both.
    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 1)
    store.note_set(tmp_path, "plans", "only", "hi")
    with pytest.raises(store.StoreError, match=r"note limit reached \(1 is the cap"):
        store.note_set(tmp_path, "plans", "second", "hi")

    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 10_000)
    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", 1)
    with pytest.raises(store.StoreError, match=r"note limit reached \(1 across all namespaces"):
        store.note_set(tmp_path, "elsewhere", "second", "hi")

    # "how full, of how much" is the figure an operator sizes a disk against, and the two
    # shifts that produce it are one character from reporting megabytes as terabytes.
    monkeypatch.setattr(store, "MAX_ROOMS", 10_000)
    monkeypatch.setattr(store, "MAX_TOTAL_ROOM_BYTES", 3 << 20)
    monkeypatch.setattr(store, "_scan", lambda *a, **k: (1, 5 << 20))  # 5 MiB on disk
    with pytest.raises(store.StoreError, match="5 MiB of a 3 MiB budget"):
        store.append(tmp_path, "overflow", "bot", "hi")


def test_an_empty_usage_file_reads_as_no_pressure(tmp_path):
    """A write cut short leaves the file there and empty. Reading that as *some* pressure
    would throttle every room to its floor on the strength of a truncated write; the
    documented default is 0, and it is the same fail-open as a missing file."""
    import store

    (tmp_path / store.USAGE_FILE).write_text("")
    assert store.room_bytes_used(tmp_path) == 0
    (tmp_path / store.USAGE_FILE).write_text("   \n")
    assert store.room_bytes_used(tmp_path) == 0


def test_the_reaper_records_room_usage_for_the_ring_to_read(tmp_path, monkeypatch):
    """The append path reads a cached total rather than walking every room per write, so
    something has to keep that total honest. The reaper already walks the tree."""
    import store

    assert store.room_bytes_used(tmp_path) == 0  # nothing recorded yet reads as no pressure

    monkeypatch.setattr(store, "REAP_EVERY", 0)  # a pass on every write, not once per 300s
    store.append(tmp_path, "somewhere", "bot", "hi")
    store.append(tmp_path, "somewhere", "bot", "again")  # this pass sees the room on disk
    before = store.room_bytes_used(tmp_path)
    assert before > 0

    for _ in range(20):
        store.append(tmp_path, "somewhere", "bot", "x" * 200)
    assert store.room_bytes_used(tmp_path) > before


def test_every_room_can_still_carry_a_topic_and_an_owner(tmp_path, monkeypatch):
    """MAX_NOTES_PER_NS >= MAX_ROOMS is only true if the *global* note cap can cover it.

    Raising MAX_ROOMS without raising MAX_NOTES_TOTAL would leave the per-namespace cap
    nominally at or above the room cap and the global cap binding first — the invariant would
    read as intact in the source and be false on disk.

    A floor rather than an equality since CHAT_MAX_NOTES_PER_NS: an operator may widen one
    namespace past the room count, and nothing about the reserved namespaces cares that they
    can. What they may not do is go under it, which is the direction this guards.
    """
    import store

    assert store.MAX_NOTES_PER_NS >= store.MAX_ROOMS
    reserved = (store.TOPIC_NS, store.OWNERS_NS, store.ALLOW_NS, store.NONCE_NS)
    assert store.MAX_NOTES_TOTAL >= len(reserved) * store.MAX_ROOMS


def test_listing_notes_does_not_evict_the_room_names(tmp_path):
    """`_listable` is memoized for the rooms walk, which asks about the same MAX_ROOMS names
    on every /rooms. Note *keys* go through the same test and there can be MAX_NOTES_PER_NS
    of them in one listing — enough to flush the cache on a single /kv/<ns> read and leave
    the walk cold, for entries nothing asks about twice. So `list_notes` calls the
    undecorated function, and this is what says so.
    """
    import store

    for i in range(20):
        store.append(tmp_path, f"room{i}", "bot", "hi")
    store.list_rooms(tmp_path)  # warms the cache with room names
    warm = store._listable.cache_info().currsize
    assert warm >= 20

    for i in range(200):
        store.note_set(tmp_path, "did", f"k{i}", "v")
    assert store.list_notes(tmp_path, "did") == sorted(f"k{i}" for i in range(200))
    assert store._listable.cache_info().currsize == warm, "a note listing must not touch it"


def test_rejected_write_leaves_no_lock_file(tmp_path, monkeypatch):
    """A cap that spends an inode per rejection is not a cap."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 1)
    store.append(tmp_path, "only", "bot", "hi")
    for i in range(5):
        with pytest.raises(store.StoreError, match="room limit"):
            store.append(tmp_path, f"flood{i}", "bot", "hi")
    assert list((tmp_path / "rooms").rglob("*.lock")) == [
        store.room_path(tmp_path, "only").with_suffix(".jsonl.lock")
    ]


def test_notes_are_capped_across_namespaces(tmp_path, monkeypatch):
    """Rotating the namespace must not buy unbounded disk: the global cap binds."""
    import store

    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", 3)
    for i in range(3):
        store.note_set(tmp_path, f"ns{i}", "k", "v")  # a fresh namespace each time
    store.note_set(tmp_path, "ns1", "k", "v2")  # overwriting an existing note still works
    with pytest.raises(store.StoreError, match="across all namespaces") as refused:
        store.note_set(tmp_path, "ns-fresh", "k", "v")
    message = str(refused.value)
    assert "fresh namespace buys nothing" in message
    assert "Overwrite a note you already own" in message and "GET /rooms" in message
    assert not (tmp_path / "notes" / "ns-fresh").exists()  # rejection creates no namespace


def test_note_cap_holds_under_concurrent_creates(tmp_path, monkeypatch):
    """Sync handlers run in a threadpool: a cap counted across files needs one gate.

    Per-key locks let N concurrent creates each count `cap - 1` and each write, so the
    documented hard cap would be soft by up to one note per in-flight request.
    """
    import threading

    import store

    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", 4)
    real_check = store._check_note_capacity

    def slow_check(root, ns_dir, path):
        real_check(root, ns_dir, path)
        time.sleep(0.02)  # widen the count→write window every racer must lose

    monkeypatch.setattr(store, "_check_note_capacity", slow_check)
    start = threading.Barrier(8)

    def create(i):
        start.wait()
        try:
            store.note_set(tmp_path, f"ns{i}", "k", "v")
        except store.StoreError:
            pass

    threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for _ in (tmp_path / "notes").rglob("*.txt")) == 4


def test_orphan_locks_are_swept(tmp_path):
    import store

    store.append(tmp_path, "gone", "bot", "hi")
    path = store.room_path(tmp_path, "gone")
    lock = path.with_suffix(".jsonl.lock")
    assert lock.exists()
    for p in (path, lock):
        _age(p, store.IDLE_SECONDS + 60)
    _arm_reaper(tmp_path)
    store.append(tmp_path, "other", "bot", "hi")  # reaps the data file, keeps its lock
    assert not path.exists()
    _arm_reaper(tmp_path)
    store.append(tmp_path, "other", "bot", "again")  # next pass sweeps the orphan lock
    assert not lock.exists()


def test_the_note_side_of_the_sweep_is_wired_up_too(tmp_path):
    """Notes are nested one directory deeper than rooms and carry a different suffix, so
    the sweep walks them with a second, hand-written tuple that nothing exercised — every
    way of getting that tuple wrong leaves note locks and empty namespaces accumulating
    forever, silently and unboundedly, on the half of the store nobody was watching."""
    import store

    store.note_set(tmp_path, "scratch", "gone", "value")
    note = store.note_path(tmp_path, "scratch", "gone")
    lock = note.with_suffix(".txt.lock")
    assert lock.exists(), "premise: note writes leave a sidecar lock"

    for target in (note, lock):
        _age(target, store.IDLE_SECONDS + 60)
    _reap_now(tmp_path)  # takes the data file, keeps the lock a writer might hold
    assert not note.exists()

    _reap_now(tmp_path)
    assert not lock.exists(), "an orphaned note lock is swept like a room's"
    # …and the namespace directory goes with the last note in it, or every namespace ever
    # written stays on disk as an empty directory.
    assert not note.parent.exists()


def test_a_lock_is_never_swept_while_its_data_file_is_there(tmp_path):
    """The sweep spares a lock whose data file still exists, whatever the lock's own age —
    a lock is touched only when someone writes, so a busy room with a quiet week looks
    exactly like an orphan. Unlinking it splits the lock domain: the next writer locks a
    fresh inode and two writers append at once."""
    import store

    store.append(tmp_path, "quiet", "bot", "hi")
    path = store.room_path(tmp_path, "quiet")
    lock = path.with_suffix(".jsonl.lock")

    _age(lock, store.IDLE_SECONDS + 60)  # the lock is stale; the room it guards is not
    _reap_now(tmp_path)

    assert path.exists() and lock.exists()


def test_cursors_survive_a_reaped_then_recreated_room(tmp_path):
    """#139: a room that is reaped and later recreated restarts seq at 1, so every reader
    still polling with a cursor from the old generation silently starves — reads answer 200
    with count 0 forever. Reaping now leaves the previous generation's high-water mark in a
    sidecar, and last_seq() consults it, so a recreated room continues the sequence and old
    cursors see the new messages. Fails before the fix: the recreated room restarts at 1 and
    the old cursor never sees anything again."""
    import store

    for i in range(6):
        store.append(tmp_path, "d-talk", "alice" if i % 2 == 0 else "bob", f"msg {i}")
    cursor = store.read_messages(tmp_path, "d-talk")["last_seq"]  # 6
    assert cursor == 6

    # Reap the room: age it past idle and force a reap pass (drop the .reaped marker gate).
    p = store.room_path(tmp_path, "d-talk")
    _age(p, store.IDLE_SECONDS + 60)
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert not p.exists(), "premise: the room was reaped"

    # Recreate it under the same name (new generation).
    store.append(tmp_path, "d-talk", "carol", "can anyone hear me?")

    result = store.read_messages(tmp_path, "d-talk", since=cursor)
    assert result["count"] > 0, "an old cursor must not starve on a recreated room"


def test_a_recreated_room_reports_a_new_generation(tmp_path):
    """#139 dir #3: a room reaped and recreated under the same name is a *different*
    conversation. The read view must expose a generation that bumps on recreate, so a
    stateful client can detect the discontinuity and resync instead of silently watching a
    new conversation under the old name. The floor bump (dir #2) keeps a stateless cursor
    fed, but only the generation tells a stateful reader the conversation changed. Fails
    before the fix: the read view carries no generation, so the recreation is
    indistinguishable from a continuation."""
    import store

    store.append(tmp_path, "d-talk", "alice", "first conversation")
    before = store.read_messages(tmp_path, "d-talk")["generation"]
    assert before == 1, "the first creation is generation 1"

    # Reap the room, then recreate it under the same name.
    p = store.room_path(tmp_path, "d-talk")
    _age(p, store.IDLE_SECONDS + 60)
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert not p.exists(), "premise: the room was reaped"

    store.append(tmp_path, "d-talk", "carol", "second conversation")
    after = store.read_messages(tmp_path, "d-talk")["generation"]
    assert after == before + 1, "recreate must bump the generation"


def test_one_unreadable_file_does_not_abort_the_whole_pass(tmp_path, monkeypatch):
    """The reaper walks every room and note in one pass, and a racing writer or a
    permission blip on any one of them is ordinary. Skipping that entry costs nothing;
    stopping the pass leaves everything after it unreaped until the next interval, which
    on a store under pressure is how a disk fills while the reaper reports success."""
    import store

    for room in ("first-idle", "second-idle"):
        store.append(tmp_path, room, "bot", "hi")
    for room in ("first-idle", "second-idle"):
        _age(store.room_path(tmp_path, room), store.IDLE_SECONDS + 60)

    def explode():
        raise OSError("racing writer")

    exploded = _race_before_lock(
        monkeypatch, store, store.room_path(tmp_path, "first-idle"), explode
    )
    _reap_now(tmp_path)

    assert exploded, "the failure never happened — this test proved nothing"
    assert not store.room_path(tmp_path, "second-idle").exists(), "the pass stopped early"


def test_reap_counts_every_room_it_takes_not_just_the_last(tmp_path):
    """The counters are the only monotonic numbers in the store, and a digest reports
    deltas from them. One reap pass usually takes many rooms; a counter that assigns
    instead of accumulating reports 1 whatever the wave size, which is exactly the signal
    a wave is supposed to produce."""
    import store

    for room in ("ended-one", "ended-two", "ended-three"):
        store.append(tmp_path, room, "bot", "hi")
        store.append(tmp_path, room, "other", "yes")  # answered, so the idle rule takes it
    for room in ("ended-one", "ended-two", "ended-three"):
        _age(store.room_path(tmp_path, room), store.IDLE_SECONDS + 60)

    before = store.counters(tmp_path)["reaped_idle"]
    _reap_now(tmp_path)
    assert store.counters(tmp_path)["reaped_idle"] == before + 3


def test_an_ephemeral_room_keeps_the_history_that_has_not_expired(tmp_path, monkeypatch):
    """Compaction retains the newest record of an `e-` room unconditionally, then stops at
    the first expired one. The guard that makes it unconditional is `and kept`, and
    dropping it turns every rotation of a *busy* ephemeral room into a truncation to one
    line — losing history that is still well inside its TTL. Only a room whose records are
    all fresh at rotation time can tell the two apart."""
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 2048)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 1024)

    for i in range(40):  # well past the ring, and all of it written just now
        store.append(tmp_path, "e-busy", "bot", f"message {i} " + "x" * 60)

    view = store.read_messages(tmp_path, "e-busy", limit=50)
    assert view["count"] > 1, "a rotating ephemeral room must keep unexpired history"
    assert view["messages"][-1]["seq"] == store.last_seq(tmp_path, "e-busy")
    # contiguous: compaction drops from the front, it never leaves a hole
    seqs = [m["seq"] for m in view["messages"]]
    assert seqs == list(range(seqs[0], seqs[-1] + 1))


def test_reap_keeps_a_file_refreshed_after_the_stat(tmp_path, monkeypatch):
    """The reaper must recheck mtime under the lock, or it deletes live messages."""
    import store

    store.append(tmp_path, "live", "bot", "hi")
    path = store.room_path(tmp_path, "live")
    _age(path, store.IDLE_SECONDS + 60)

    def refresh(target):
        os.utime(target, None)  # a writer got in between the stat and the unlink

    _race_under_lock(monkeypatch, store, refresh)
    _reap_now(tmp_path)
    assert path.exists()


def test_reap_keeps_a_note_refreshed_after_the_stat(tmp_path, monkeypatch):
    """The same recheck, on the nested half of the walk.

    Rooms and notes are two passes of one loop over one `_walk`, and only the room pass was
    covered. The trap this guards is that `os.DirEntry.stat()` caches: the reaper stats once
    to decide a file is idle and again under the lock to catch a writer who got in between,
    and a recheck reading the cached value silently returns the pre-lock answer. That is not
    a slower reap, it is a deleted note somebody had just written — so it is pinned on both
    branches rather than on whichever one happened to have a test.
    """
    import store

    store.note_set(tmp_path, "plans", "k", "v")
    path = store.note_path(tmp_path, "plans", "k")
    _age(path, store.IDLE_SECONDS + 60)

    def refresh(target):
        if os.fspath(target) == os.fspath(path):  # only the note under test
            os.utime(target, None)

    _race_under_lock(monkeypatch, store, refresh)
    _reap_now(tmp_path)
    assert path.exists(), "a note refreshed between the walk and the lock must survive"
    assert store.note_get(tmp_path, "plans", "k") == "v"


def test_trusting_every_peer_would_hand_the_caller_its_own_rate_limit_identity():
    """What the flag above buys, demonstrated rather than asserted from memory: the same
    remote peer, the same header, the two trust settings. Pinning the failure mode means a
    future uvicorn that changes this behaviour is caught here rather than in production."""
    import asyncio
    from typing import Any, cast

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen: dict[str, tuple] = {}

    async def sink(scope, receive, send):
        seen["client"] = scope["client"]

    def client_seen_by_app(trusted: str) -> str:
        scope = {
            "type": "http",
            "client": ("203.0.113.9", 54321),  # an ordinary caller, not a proxy
            "scheme": "http",
            "headers": [(b"x-forwarded-for", b"1.2.3.4"), (b"host", b"x")],
        }
        # A hand-built scope and no receive/send: this probes the middleware's rewrite step,
        # which reads `client` and `headers` and forwards the rest untouched to `sink`. Cast
        # because the real signature wants full ASGI callables that nothing here calls.
        mw = cast(Any, ProxyHeadersMiddleware(sink, trusted_hosts=trusted))
        asyncio.run(mw(scope, None, None))
        return seen["client"][0]

    assert client_seen_by_app("*") == "1.2.3.4"  # what the image used to do
    assert client_seen_by_app("127.0.0.1") == "203.0.113.9"  # real peer survives


def test_idle_rooms_are_reaped_so_squatting_expires(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 2)
    store.append(tmp_path, "squat", "bot", "hi")
    _age(store.room_path(tmp_path, "squat"), store.IDLE_SECONDS + 60)
    _arm_reaper(tmp_path)  # force a reap pass
    store.append(tmp_path, "fresh", "bot", "hi")
    assert not store.room_path(tmp_path, "squat").exists()
    assert store.room_path(tmp_path, "fresh").exists()


def test_stillborn_rooms_go_after_a_day_but_answered_ones_keep_the_week(tmp_path):
    """One message nobody answered is worth a day; a conversation that stopped is worth a
    week. Both rooms are idle for the same time — only the reply tells them apart."""
    import store

    store.append(tmp_path, "monologue", "bot", "anyone here?")
    store.append(tmp_path, "answered", "bot", "anyone here?")
    store.append(tmp_path, "answered", "other", "yes")
    for room in ("monologue", "answered"):
        _age(store.room_path(tmp_path, room), store.STILLBORN_SECONDS + 60)
    _reap_now(tmp_path)
    assert not store.room_path(tmp_path, "monologue").exists()
    assert store.room_path(tmp_path, "answered").exists()


def test_stillborn_room_survives_its_first_day(tmp_path):
    """The rule is 24h of silence, not "one message is disposable" — a room posted into an
    hour ago is exactly what a slow rendezvous looks like."""
    import store

    store.append(tmp_path, "waiting", "bot", "anyone here?")
    _age(store.room_path(tmp_path, "waiting"), 3600)
    _reap_now(tmp_path)
    assert store.room_path(tmp_path, "waiting").exists()


def test_stillborn_rule_does_not_touch_notes(tmp_path):
    """A note has no reply to wait for, so a single write says nothing about it. Notes keep
    the 7-day rule, and a topic must outlive the first day of the room it describes."""
    import store

    store.note_set(tmp_path, store.TOPIC_NS, "somewhere", "what this room is for")
    path = store.note_path(tmp_path, store.TOPIC_NS, "somewhere")
    _age(path, store.STILLBORN_SECONDS + 60)
    _reap_now(tmp_path)
    assert path.exists()


def test_a_torn_line_does_not_make_a_busy_room_look_stillborn(tmp_path):
    """The stillborn count skips what it cannot parse rather than stopping at it: stopping
    reads a room with one bad line as a room with no messages, and the reaper takes a
    conversation because of a byte a crash left behind. From the mutation run — turning
    that `continue` into a `break` passed the whole suite."""
    import store

    path = store.room_path(tmp_path, "torn")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b'{"seq":1,"ts":"2026-01-01T00:00:00.000000Z","from":"a","tex\n'  # cut mid-record
        b'{"seq":2,"ts":"2026-01-01T00:00:01.000000Z","from":"a","text":"anyone here?"}\n'
        b'{"seq":3,"ts":"2026-01-01T00:00:02.000000Z","from":"b","text":"yes"}\n'
    )
    _age(path, store.STILLBORN_SECONDS + 60)
    _reap_now(tmp_path)

    assert path.exists(), "two answered messages and a torn line is not a monologue"
    # …and the messages either side of the torn line are still readable.
    assert store.read_messages(tmp_path, "torn")["count"] == 2


def test_a_room_that_cannot_be_counted_is_never_stillborn(tmp_path):
    """Fail open, and only here: a reaper that reads "I could not count this" as "there is
    nothing here" deletes live data on the first IO error it meets."""
    import store

    unreadable = tmp_path / "rooms"  # a directory: opening it raises, like a bad file
    unreadable.mkdir()
    assert store._stillborn(unreadable) is False


def test_a_second_precision_timestamp_still_expires(tmp_path):
    """Records predating microsecond `ts` carry `...:05Z`, and expiry is the only thing
    that parses `ts` — so the older form must keep working or an `e-` room silently stops
    expiring its oldest records. Both forms coexist by design; this keeps the second real."""
    from datetime import UTC, datetime, timedelta

    import store

    path = store.room_path(tmp_path, "e-legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = datetime.now(UTC) - timedelta(seconds=store.EPHEMERAL_TTL_SECONDS + 60)
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    path.write_bytes(
        f'{{"seq":1,"ts":"{stale.strftime("%Y-%m-%dT%H:%M:%SZ")}","from":"a","text":"old"}}\n'
        f'{{"seq":2,"ts":"{fresh.strftime("%Y-%m-%dT%H:%M:%SZ")}","from":"a","text":"new"}}\n'.encode()
    )
    view = store.read_messages(tmp_path, "e-legacy")
    assert [m["text"] for m in view["messages"]] == ["new"]
    # seq keeps advancing past what nobody can read any more, or a cursor would be reused.
    assert store.last_seq(tmp_path, "e-legacy") == 2


def test_reap_spares_a_stillborn_room_answered_after_the_count(tmp_path, monkeypatch):
    """The under-lock recheck must re-count, not just re-stat: a reply landing mid-pass is
    exactly the message the reaper would otherwise delete."""
    import store

    store.append(tmp_path, "racing", "bot", "anyone here?")
    path = store.room_path(tmp_path, "racing")
    _age(path, store.STILLBORN_SECONDS + 60)

    def answer(target):
        with target.open("ab") as f:  # a reply got in between the count and the unlink
            f.write(b'{"seq":2,"ts":"2026-01-01T00:00:00Z","from":"other","text":"yes"}\n')
        _age(target, store.STILLBORN_SECONDS + 60)  # still idle: only the count saves it

    _race_under_lock(monkeypatch, store, answer)
    _reap_now(tmp_path)
    assert path.exists()


def test_reverse_lines_reads_only_the_tail(tmp_path):
    import store

    p = tmp_path / "x.jsonl"
    p.write_bytes(b"".join(b'{"seq":%d}\n' % i for i in range(50_000)))
    with p.open("rb") as f:
        first = next(store.reverse_lines(f))
    assert first == b'{"seq":49999}'


def test_torn_final_line_costs_only_that_record(tmp_path):
    """The crash-recovery claim: a half-written last line must not poison the file."""
    import store

    for i in range(5):
        store.append(tmp_path, "crash", "bot", f"m{i}")
    path = store.room_path(tmp_path, "crash")
    with path.open("ab") as f:
        f.write(b'{"seq":6,"ts":"2026-01-01T00:00:00Z","from":"bot","te')  # power loss here
    view = store.read_messages(tmp_path, "crash", limit=50)
    assert [m["text"] for m in view["messages"]] == [f"m{i}" for i in range(5)]
    store.append(tmp_path, "crash", "bot", "after")  # and writing still works
    assert store.read_messages(tmp_path, "crash", limit=1)["messages"][0]["text"] == "after"


def test_concurrent_appends_never_duplicate_a_seq(tmp_path):
    import threading

    import store

    def hammer():
        for _ in range(40):
            store.append(tmp_path, "race", "bot", "x")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    view = store.read_messages(tmp_path, "race", limit=store.MAX_LIMIT)
    seqs = [m["seq"] for m in view["messages"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert store.last_seq(tmp_path, "race") == 160


def test_notes_per_namespace_are_capped(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 2)
    store.note_set(tmp_path, "ns", "a", "1")
    store.note_set(tmp_path, "ns", "b", "2")
    store.note_set(tmp_path, "ns", "a", "overwrite is fine")  # existing key still writable
    with pytest.raises(store.StoreError, match="note limit"):
        store.note_set(tmp_path, "ns", "c", "3")
    store.note_set(tmp_path, "other", "c", "3")  # cap is per namespace, not global
    assert store.note_get(tmp_path, "ns", "a") == "overwrite is fine"


def test_reaper_spares_active_files_and_throttles_itself(tmp_path, monkeypatch):
    import store

    store.append(tmp_path, "active", "bot", "hi")
    store.note_set(tmp_path, "ns", "keep", "value")
    store.append(tmp_path, "other", "bot", "hi")
    assert store.room_path(tmp_path, "active").exists()
    assert store.note_get(tmp_path, "ns", "keep") == "value"

    # a reap ran on the first write, so the marker exists and the next pass is throttled
    marker = tmp_path / ".reaped"
    assert marker.exists()
    _age(store.room_path(tmp_path, "other"), store.IDLE_SECONDS + 60)
    store.append(tmp_path, "active", "bot", "again")
    assert store.room_path(tmp_path, "other").exists()  # throttled: not reaped yet
    marker.unlink()
    store.append(tmp_path, "active", "bot", "third")
    assert not store.room_path(tmp_path, "other").exists()  # now it is


def test_engagement_flags_a_room_only_one_nick_ever_wrote_in(tmp_path):
    """The Moltbook 93.5% analog: nobody ever answered, so every message is unanswered."""
    import store

    for i in range(5):
        store.append(tmp_path, "monologue", "solo", f"m{i}")
    row = _stats_for(tmp_path, "monologue")
    assert row["window"] == 5
    assert row["zero_response_share"] == 1.0
    assert row["nick_diversity"] == 0.2  # 1 nick / 5 messages — the floor for this window


def test_engagement_counts_a_message_as_answered_only_if_a_different_nick_follows(tmp_path):
    import store

    for nick in ("a", "a", "b", "b", "b"):  # oldest first
        store.append(tmp_path, "talk", nick, "hi")
    row = _stats_for(tmp_path, "talk")
    # a's two messages are both followed by b; b's three are followed only by b
    assert row["zero_response_share"] == 0.6
    assert row["nick_diversity"] == 0.4  # 2 distinct nicks / 5 messages


def test_engagement_window_binds_before_the_ring_does(tmp_path, monkeypatch):
    """The metrics are over the scanned window, not over room history — so a room whose
    older half looks different must score on the window, and say how big it was."""
    import store

    for i in range(7):
        store.append(tmp_path, "shift", "alice", f"m{i}")
    for i in range(5):
        store.append(tmp_path, "shift", "bob", f"n{i}")
    monkeypatch.setattr(store, "WINDOW_MESSAGES", 5)
    row = _stats_for(tmp_path, "shift")
    assert row["window"] == 5 and row["last_seq"] == 12  # window < ring, cursor still exact
    assert row["zero_response_share"] == 1.0  # the newest 5 are all bob's
    assert row["nick_diversity"] == 0.2


def test_listings_never_echo_a_name_the_validator_would_reject(tmp_path):
    """Defence in depth for anything already on disk: a hand-created file with a newline
    in its name must not be echoed into a response and forge a line."""
    import store

    (tmp_path / "rooms").mkdir(parents=True)
    (tmp_path / "rooms" / "ok.jsonl").write_bytes(b'{"seq":1,"ts":"t","from":"b","text":"x"}\n')
    (tmp_path / "rooms" / "bad\nname.jsonl").write_bytes(b'{"seq":1}\n')
    (tmp_path / "rooms" / "UPPER.jsonl").write_bytes(b'{"seq":1}\n')
    assert store.list_rooms(tmp_path) == ["ok"]
    assert [r["room"] for r in store.room_stats(tmp_path)["rooms"]] == ["ok"]


def test_old_second_precision_records_still_parse(tmp_path, monkeypatch):
    """Records written before microsecond timestamps must keep reading — `ts` is opaque."""
    import store

    room = store.room_path(tmp_path, "legacy")
    room.parent.mkdir(parents=True, exist_ok=True)
    room.write_text('{"seq":1,"ts":"2026-01-01T00:00:00Z","from":"old","text":"hi"}\n')
    view = store.read_messages(tmp_path, "legacy")
    assert view["messages"][0]["text"] == "hi" and view["last_seq"] == 1
    store.append(tmp_path, "legacy", "new", "next")  # and appending after them works
    assert store.read_messages(tmp_path, "legacy")["last_seq"] == 2


def test_a_failed_announcement_never_fails_the_write(tmp_path, monkeypatch):
    """The caller's message is already fsynced when the event is written."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 1)
    rec = store.append(tmp_path, "solo", "bot", "hi")  # events cannot fit under the cap
    assert rec["seq"] == 1  # the user's write still succeeded
    assert not store.room_path(tmp_path, "events").exists()


def test_ownership_guards_do_not_expire_out_from_under_a_live_room(tmp_path):
    """room-owners, room-allow and room-nonce were reaped on their own mtime, so 7 quiet
    days of *ownership* opened a still-busy room to a fresh claim, silently dropped the
    allow-list, and reset the counter that stops a captured URL re-adding a revoked key."""
    import store

    did = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    store.append(tmp_path, "d-live", "bot", "hi")
    for ns, value in ((store.OWNERS_NS, did), (store.ALLOW_NS, did), (store.NONCE_NS, "7")):
        store.note_set(tmp_path, ns, "d-live", value)
        _age(store.note_path(tmp_path, ns, "d-live"), store.IDLE_SECONDS + 60)

    _arm_reaper(tmp_path)
    store.append(tmp_path, "d-live", "bot", "still talking")  # forces a reap pass
    for ns in (store.OWNERS_NS, store.ALLOW_NS, store.NONCE_NS):
        assert store.note_get(tmp_path, ns, "d-live") is not None, ns

    # once the room itself goes, the guards go with it — bounded exactly as before
    _age(store.room_path(tmp_path, "d-live"), store.IDLE_SECONDS + 60)
    _arm_reaper(tmp_path)
    store.append(tmp_path, "elsewhere", "bot", "hi")
    assert not store.room_path(tmp_path, "d-live").exists()
    _arm_reaper(tmp_path)
    store.append(tmp_path, "elsewhere", "bot", "again")
    for ns in (store.OWNERS_NS, store.ALLOW_NS, store.NONCE_NS):
        assert store.note_get(tmp_path, ns, "d-live") is None, ns


def test_ephemeral_expiry_is_lazy_but_rotation_reclaims_the_disk(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 4096)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 2048)
    real_now = store._now
    _at(monkeypatch, store, "2020-01-01T00:00:00.000000Z")
    for _ in range(40):
        store.append(tmp_path, "e-chat", "bot", "x" * 100)
    monkeypatch.setattr(store, "_now", real_now)
    for _ in range(30):
        store.append(tmp_path, "e-chat", "bot", "fresh")
    view = store.read_messages(tmp_path, "e-chat", limit=200)
    assert {m["text"] for m in view["messages"]} == {"fresh"}
    assert view["last_seq"] == 70 and view["first_seq"] > 40  # seq never rewinds; gap visible
    disk = store.room_path(tmp_path, "e-chat").read_text()
    assert "2020-01-01" not in disk  # rotation reclaimed the expired records
    assert store.room_path(tmp_path, "e-chat").stat().st_size <= 4096


@pytest.mark.parametrize("stamp", ["whenever", None, 0, {}, []])
def test_an_unparseable_timestamp_counts_as_expired(tmp_path, stamp):
    """Fail closed for malformed JSON types as well as malformed timestamp strings.

    The room file is persistent attacker-controlled input after any volume restore or manual
    repair; accepting a non-string here would silently violate the advertised deletion age.
    """
    import store

    room = store.room_path(tmp_path, "e-x")
    room.parent.mkdir(parents=True, exist_ok=True)
    room.write_text(json.dumps({"seq": 1, "ts": stamp, "from": "bot", "text": "hi"}) + "\n")
    assert store.read_messages(tmp_path, "e-x")["count"] == 0
    assert store.read_messages(tmp_path, "keeps-it")["count"] == 0  # a different room, empty


def test_ephemeral_ttl_boundary_is_inclusive_then_expires(tmp_path, monkeypatch):
    """At exactly TTL the record is still within the promise; one microsecond older is not.

    The paired timestamps lock the retention contract to its comparison boundary: a timestamp
    equal to the cutoff is retained, while the immediately preceding microsecond is expired.
    """
    from datetime import UTC, datetime

    import store

    now = 2_000_000_000.0
    cutoff = now - store.EPHEMERAL_TTL_SECONDS

    def stamp(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    room = store.room_path(tmp_path, "e-boundary")
    room.parent.mkdir(parents=True, exist_ok=True)
    records = (
        {"seq": 1, "ts": stamp(cutoff - 0.000001), "from": "bot", "text": "expired"},
        {"seq": 2, "ts": stamp(cutoff), "from": "bot", "text": "exact"},
    )
    room.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setattr(store.time, "time", lambda: now)

    view = store.read_messages(tmp_path, "e-boundary")
    assert [message["text"] for message in view["messages"]] == ["exact"]
    assert view["first_seq"] == 2 and view["last_seq"] == 2


def test_message_counter_survives_the_reaper(tmp_path):
    """The reason the counter exists. Summing per-room `last_seq` would report 0 here, so a
    digest's "messages since last time" would go *negative* every time a room is reaped."""
    import store

    for i in range(3):
        store.append(tmp_path, "doomed", "bot", f"m{i}")
    assert store.counters(tmp_path)["messages"] == 3

    for room in ("doomed", "events"):
        _age(store.room_path(tmp_path, room), store.IDLE_SECONDS + 60)
    _reap_now(tmp_path)

    assert not store.room_path(tmp_path, "doomed").exists()
    assert store.counters(tmp_path)["messages"] == 3  # monotonic across the deletion
    assert store.counters(tmp_path)["rooms_created"] == 1


def test_reap_counters_tell_the_two_rules_apart(tmp_path):
    """A wave of stillborn reaps means openers nobody answered; a wave of idle reaps means
    conversations that ended. One counter for both would hide the difference that matters."""
    import store

    store.append(tmp_path, "monologue", "bot", "anyone here?")
    store.append(tmp_path, "ended", "bot", "hi")
    store.append(tmp_path, "ended", "other", "bye")
    _age(store.room_path(tmp_path, "monologue"), store.STILLBORN_SECONDS + 60)
    _age(store.room_path(tmp_path, "ended"), store.IDLE_SECONDS + 60)
    _reap_now(tmp_path)

    counts = store.counters(tmp_path)
    assert (counts["reaped_stillborn"], counts["reaped_idle"]) == (1, 1)


def test_message_counter_survives_compaction(tmp_path, monkeypatch):
    """Compaction drops old lines from the ring, so what is on disk is not what was said."""
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 2048)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 1024)
    for i in range(60):
        store.append(tmp_path, "busy", "bot", f"message number {i} with some padding text")
    on_disk = sum(1 for _ in store.room_path(tmp_path, "busy").open("rb"))
    assert on_disk < 60  # the ring dropped lines
    assert store.counters(tmp_path)["messages"] == 60  # the counter did not


def test_snapshots_accumulate_on_the_write_path_without_a_background_thread(tmp_path, monkeypatch):
    """The history the digest differences against. Taken by whoever writes next, if due —
    the same throttle idiom as the reaper, so the service still runs no scheduler."""
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)  # every write is due
    for i in range(3):
        store.append(tmp_path, "lobby", "bot", f"m{i}")
    history = store.snapshots(tmp_path)
    assert len(history) == 3
    assert [h["counters"]["messages"] for h in history] == [1, 2, 3]
    assert all(isinstance(h["t"], int) for h in history)


def test_snapshots_are_throttled_so_a_burst_costs_one_sample(tmp_path):
    """SNAPSHOT_EVERY is 300s by default: a hundred messages in a minute must not write a
    hundred aggregate walks."""
    import store

    for i in range(20):
        store.append(tmp_path, "lobby", "bot", f"m{i}")
    assert len(store.snapshots(tmp_path)) == 1


def test_snapshots_prune_past_the_retention_window(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    store.append(tmp_path, "lobby", "bot", "old")
    path = tmp_path / store.SNAPSHOTS_FILE
    stale = json.loads(path.read_text().splitlines()[0])
    stale["t"] = int(time.time() - store.SNAPSHOT_KEEP_SECONDS - 3600)
    path.write_text(json.dumps(stale) + "\n")
    store.append(tmp_path, "lobby", "bot", "new")
    kept = store.snapshots(tmp_path)
    assert len(kept) == 1 and kept[0]["t"] > stale["t"]


def test_snapshots_survive_a_torn_line(tmp_path, monkeypatch):
    """Losing the sample a kill -9 was mid-write on is fine; losing the history behind it
    is not."""
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    store.append(tmp_path, "lobby", "bot", "hi")
    path = tmp_path / store.SNAPSHOTS_FILE
    path.write_text(path.read_text() + '{"t": 1, "coun')
    assert len(store.snapshots(tmp_path)) == 1


def test_corrupt_aggregate_metadata_is_ignored_without_inventing_usage(tmp_path):
    """Counters and snapshots are diagnostics, never authority. A corrupt sidecar must not
    take down writes or be interpreted as a huge/negative value that changes enforcement.
    """
    import store

    (tmp_path / store.COUNTERS_FILE).write_text("[]")
    assert store.counters(tmp_path) == dict.fromkeys(store.COUNTER_KEYS, 0)

    samples = tmp_path / store.SNAPSHOTS_FILE
    samples.write_text(
        "\n".join((json.dumps({"t": "yesterday"}), json.dumps([1, 2]), json.dumps({"t": 7})))
    )
    assert store.snapshots(tmp_path) == [{"t": 7}]


def test_fsync_is_a_knob_but_compaction_never_skips_it(tmp_path, monkeypatch):
    """CHAT_FSYNC=0 trades the per-message fsync for write headroom: a host crash can lose
    the final moments of appends, and torn-tail healing already prices a cut-short write at
    one record. Compaction is not part of the trade — os.replace of a file whose bytes never
    reached disk can lose the room's whole retained ring, so it pays the fsync either way."""
    import config
    import store

    real = os.fsync
    calls = []

    def counted(fd):
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(store.os, "fsync", counted)

    store.append(tmp_path, "lobby", "bot", "durable")
    # Four, not two: creating the lobby room and its /r/events announcement each pay one
    # fsync for the record and one for the seq-state metadata (generation/floor) the reaper
    # leaves behind on a later reap (#139). The knob governs both.
    assert len(calls) == 4

    with config.override(FSYNC=False):
        store.append(tmp_path, "lobby", "bot", "fast")
        assert len(calls) == 4  # neither the append nor a seq-state write paid one
        store._compact(store.room_path(tmp_path, "lobby"))
        assert len(calls) == 5  # the rewrite did not


def test_room_windows_are_memoized_against_the_stat_the_walk_already_does(tmp_path, monkeypatch):
    """A write changes one room's (mtime_ns, size), so the overview re-reads that room's
    tail and reuses every other window from the memo — O(changed), not O(shown)."""
    import store

    store.append(tmp_path, "aaa", "bot", "one")
    store.append(tmp_path, "bbb", "bot", "two")
    calls = []
    real = store.room_window
    monkeypatch.setattr(
        store, "room_window", lambda root, name: (calls.append(name), real(root, name))[1]
    )

    store.room_stats(tmp_path)
    first = sorted(calls)
    store.room_stats(tmp_path)
    assert sorted(calls) == first, "an unchanged room must not be re-read"

    store.append(tmp_path, "aaa", "bot", "again")  # aaa's second message
    calls.clear()
    view = store.room_stats(tmp_path)
    assert calls == ["aaa"], "only the changed room is re-read"
    assert {r["room"]: r["last_seq"] for r in view["rooms"]}["aaa"] == 2

    monkeypatch.setattr(store, "_WINDOW_MEMO_MAX", 1)
    store.append(tmp_path, "aaa", "bot", "third-message")
    store.room_stats(tmp_path)
    assert len(store._window_memo) == 1  # the bound holds under eviction


def test_topic_previews_ride_the_notes_counter_not_only_a_clock(tmp_path):
    """A topic set is a note write, so it bumps notes_written and shows up immediately;
    a deletion the counter cannot see (the reaper's) ages out with the TTL."""
    import config
    import store

    def topics():
        return {r["room"]: r["topic"] for r in store.room_stats(tmp_path)["rooms"]}

    store.append(tmp_path, "aaa", "bot", "hello")
    assert topics()["aaa"] is None
    store.note_set(tmp_path, store.TOPIC_NS, "aaa", "what aaa is for")
    assert topics()["aaa"] == "what aaa is for"

    store.note_path(tmp_path, store.TOPIC_NS, "aaa").unlink()  # a reaper-style deletion
    with config.override(NOTE_STATS_CACHE_SECONDS=0):
        assert topics()["aaa"] is None  # visible once the clock (here: disabled) expires


def test_cached_topic_survives_interleaved_generations_without_thrashing(tmp_path, monkeypatch):
    """room_stats is reached through a sync `def` route that Starlette runs in a
    threadpool, so two /rooms callers straddling one topic write are not a hypothetical:
    they see different stamps and their per-room lookups can land on the cache in any
    order relative to each other. Before this cache was keyed per stamp, it was a single
    mutable slot any mismatched stamp reset unconditionally — so two interleaved
    generations did not just miss each other's entries once, every remaining lookup in
    both loops re-triggered a reset, and the note reads this cache exists to bound
    happened on every call instead of once per (stamp, room).

    This reproduces the interleaving directly (no real threads needed: the bug is in the
    cache's logic, not in the GIL) and checks the discriminating property — a second,
    identical pass over both generations must be served entirely from cache. Revert
    _cached_topic to a single slot and this fails: the second pass re-reads everything,
    because the two generations were still fighting over one slot.
    """
    import store

    reads = []

    def counted_topic(root, room):
        reads.append(room)
        return f"topic-for-{room}"

    monkeypatch.setattr(store, "topic", counted_topic)
    store._topics_memo.clear()

    rooms = [f"room{i}" for i in range(5)]
    stamp_a, stamp_b = ((1,), str(tmp_path)), ((2,), str(tmp_path))
    now = 1_000_000.0

    # Two /rooms loops, interleaved room-by-room — the order a threadpool executing both
    # requests concurrently can produce.
    for room in rooms:
        store._cached_topic(tmp_path, room, stamp_a, now)
        store._cached_topic(tmp_path, room, stamp_b, now)
    assert len(reads) == len(rooms) * 2  # first pass: everything is a genuine miss

    reads.clear()
    for room in rooms:
        store._cached_topic(tmp_path, room, stamp_a, now)
        store._cached_topic(tmp_path, room, stamp_b, now)
    assert reads == [], "a second identical pass over both generations must hit cache"


def test_a_json_escaped_did_is_the_one_record_the_nonce_scan_cannot_see(tmp_path):
    """The stated boundary of `_last_nonce`'s bytes-level reject, not a wish.

    The reject assumes the DID is in the line as itself. Both encoders this store has ever
    written rooms with put it there literally — test_json_backend.py pins that byte-for-byte
    — so the only way to produce the record below is to write the file with something else.
    `_parse` still yields the right `from`, and the scan still skips it, which means a replay
    of that record's nonce is accepted while the record sits in the window.

    That is a real narrowing, kept deliberately: covering it costs a second scan of every
    line (2.1 ms -> 3.7 ms against a 4.1 ms baseline on tests/capacity_bench.py), which is
    most of what the reject buys, to defend files this store did not write. Make the scan
    escape-aware and this test is what tells you: delete it and pin the opposite.
    """
    import didkey
    import store

    did, _ = _keypair()
    assert didkey.is_did(did)  # a key the verifier would accept, not a did-shaped string
    escaped = "".join(f"\\u{ord(c):04x}" for c in did)
    room = store.room_path(tmp_path, "lobby")
    room.parent.mkdir(parents=True)
    room.write_bytes(
        b'{"seq":1,"ts":"t","from":"' + escaped.encode() + b'","text":"signed","nonce":7}\n'
    )
    rec = store._parse(room.read_bytes())
    assert rec is not None and rec["from"] == did  # legal JSON, and it parses to the DID
    assert did.encode() not in room.read_bytes()  # but not present as itself, so:
    assert store._last_nonce(tmp_path, "lobby", did) is None
