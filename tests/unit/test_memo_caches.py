"""Run: uv run --group dev python -m pytest tests

The three memo caches behind /rooms — store._cached_window, store._topics_memo and
app._rooms_walk — under the conditions that used to break them. `rooms` is a sync `def`
route, so Starlette runs it in a real thread pool, and every one of these caches is
module-level state several OS threads reach at the same moment.

All three answer "is this entry still current?" by putting the answer in the key: the
room's stat, the counter stamp and the time bucket are key material, so a superseded entry
is not something to find and invalidate, it is a key nobody looks up any more. That is what
retires the bug class these tests are about. There is no promotion after a hit and no
insert-then-evict after a miss for another thread to land in the middle of (#376, #229),
and there is no single shared slot two callers can reset out from under each other (#515).
What the LRU keeps coherent under concurrent calls, it keeps coherent itself — nothing here
argues from GIL scheduling.

The concurrency tests do not hope for an interleaving. They gate the *producer* — the
function a cache calls on a miss — on a barrier, which parks every worker inside a miss
before any of them returns, over a cache already filled to its bound so an eviction is in
flight while the others insert. Once the workers stop arriving together the barrier breaks
on its timeout and they carry on; that is the pass condition, not a failure, exactly as in
tests/unit/test_token_bucket.py. Nothing here sleeps.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import app  # noqa: E402
import config  # noqa: E402
import store  # noqa: E402

WORKERS = 6
# One timeout is the whole cost of the guarded path, because the gate arms once. It only
# has to outlast the thread starts it is waiting on, never any I/O.
GATE_TIMEOUT = 0.5
# A stamp and a clock the tests choose, so nothing here depends on a counter file or on
# what the machine's monotonic clock happened to read.
STAMP = ("stamp", 1)
NOW = 1_000.0


class _Gated:
    """A stub producer that returns only once every worker has reached it.

    A miss is where a cache does its bookkeeping, so parking every worker inside one at the
    same moment is what puts inserts and an eviction in flight together. `arm` opens that
    window once, for the next WORKERS calls; the calls before it (the fill) and after it
    (whatever the workers race through) run straight past. If one worker takes two of those
    slots the barrier is never satisfied and breaks on its timeout instead, which is the
    same pass condition and costs the timeout once, not once per call.
    """

    def __init__(self, value):
        self._value = value
        self._counting = threading.Lock()
        self._barrier = threading.Barrier(WORKERS)
        self.calls = 0
        self._parking = 0

    def arm(self) -> None:
        self._parking = WORKERS

    def __call__(self, *args):
        with self._counting:
            self.calls += 1
            park = self._parking > 0
            self._parking -= park
        if park:
            try:
                self._barrier.wait(timeout=GATE_TIMEOUT)
            except threading.BrokenBarrierError:
                pass  # serialised, so the others are not coming: carry on
        return self._value(*args)


def _in_parallel(work) -> None:
    """Run `work()` on WORKERS threads at once and re-raise whatever any of them raised."""
    failures: list[Exception] = []
    collecting = threading.Lock()
    ready = threading.Barrier(WORKERS)

    def run() -> None:
        try:
            ready.wait(timeout=GATE_TIMEOUT)
        except threading.BrokenBarrierError:
            pass
        try:
            work()
        except Exception as exc:
            with collecting:
                failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise failures[0]


# --------------------------------------------------------- eviction under contention


def test_the_window_memo_answers_every_worker_while_it_is_evicting(tmp_path, monkeypatch):
    """#376/#229: a valid hit whose key another thread evicted between the lookup and the
    promotion that followed it. There is no promotion left to raise, and the check is not
    merely that nothing raised — each worker asserts the window it got back belongs to the
    key it asked for, because a bound that quietly swapped two entries would be the same
    bug with a worse symptom.
    """
    store._cached_window.cache_clear()
    root = str(tmp_path)
    bound = store._WINDOW_MEMO_MAX
    gate = _Gated(lambda _root, name: (int(name[1:]), [name]))
    monkeypatch.setattr(store, "room_window", gate)
    for n in range(bound):  # full, so every miss below has to evict something
        store._cached_window(root, f"r{n}", (n, n))
    gate.arm()

    def work() -> None:
        for n in range(bound + WORKERS * 4):
            assert store._cached_window(root, f"r{n}", (n, n)) == (n, (f"r{n}",))

    _in_parallel(work)
    info = store._cached_window.cache_info()
    assert info.currsize == bound, "the bound is what holds, and it held"
    assert gate.calls > bound, "the workers asked for more windows than the cache can hold"


def test_the_topic_memo_answers_every_worker_while_it_is_evicting(tmp_path, monkeypatch):
    """The same pressure on the cache that used to be one mutable slot: what is bounded now
    is a real LRU, so the interesting failure is eviction rather than a reset, and the
    per-room key is what has to keep pointing at the right room through it."""
    store._topics_memo.cache_clear()
    root = str(tmp_path)
    bound = store._TOPICS_MEMO_MAX
    gate = _Gated(lambda _root, room: f"topic of {room}")
    monkeypatch.setattr(store, "topic", gate)
    for n in range(bound):
        store._cached_topic(root, f"r{n}", STAMP, NOW)
    gate.arm()

    def work() -> None:
        for n in range(bound + WORKERS * 4):
            assert store._cached_topic(root, f"r{n}", STAMP, NOW) == f"topic of r{n}"

    _in_parallel(work)
    assert store._topics_memo.cache_info().currsize == bound
    assert gate.calls > bound


def test_the_rooms_cache_answers_every_worker_while_it_is_evicting(monkeypatch):
    """And the one the pop-then-insert comment used to live on. The walk is stubbed because
    what is under test is the cache, not the store: 64 entries of pressure would otherwise
    be 64 directory walks, and the stub makes a mismatched answer visible instead of just
    slow."""
    app._rooms_walk.cache_clear()
    bound = app.MAX_ROOMS_CACHE
    gate = _Gated(lambda limit: {"limit": limit})
    monkeypatch.setattr(app, "_rooms_payload", gate)
    for n in range(bound):
        app._rooms_walk(n, STAMP, 0)
    gate.arm()

    def work() -> None:
        for n in range(bound + WORKERS * 4):
            assert app._rooms_walk(n, STAMP, 0) == {"limit": n}

    _in_parallel(work)
    assert app._rooms_walk.cache_info().currsize == bound
    assert gate.calls > bound


# ------------------------------------------------------------------- the #515 thrash


def test_concurrent_callers_share_the_topic_cache_instead_of_thrashing_it(tmp_path, monkeypatch):
    """#515: the single slot was reset by any caller whose stamp did not match it.

    Two /rooms requests straddling one topic write did not merely miss each other once.
    Each makes one lookup per shown room, and every one of those lookups reset the slot the
    other was using, so neither ever hit and both re-read every topic — the cost growing
    with the room count and the caller count together. The measurement is a warm cache and
    then a burst: with two generations already computed, a room's topic must not be read
    again, however many threads ask for it and whichever generation they ask under.
    """
    store._topics_memo.cache_clear()
    root = str(tmp_path)
    rooms = [f"r{n}" for n in range(24)]
    generations = (("stamp", 1), ("stamp", 2))
    gate = _Gated(lambda _root, room: f"topic of {room}")
    monkeypatch.setattr(store, "topic", gate)

    def pass_over_both_generations() -> None:
        # Room-major, so the two generations alternate on every single lookup rather than
        # in two blocks. That is the order the reported thrash was in — two threadpool
        # requests interleaving room by room — and the order in which one slot is at its
        # worst: under the old cache this reset it 48 times in a pass and hit zero times.
        for room in rooms:
            for stamp in generations:
                assert store._cached_topic(root, room, stamp, NOW) == f"topic of {room}"

    pass_over_both_generations()
    warm = gate.calls
    assert warm == len(rooms) * len(generations), "one read per room per generation, cold"

    _in_parallel(pass_over_both_generations)
    assert gate.calls == warm, "a warm topic cache must not re-read one topic, ever"


# ------------------------------------------------------------------------- staleness


def test_a_write_moves_the_key_so_the_very_next_read_is_the_fresh_view(tmp_path):
    """Validity-in-key does not soften the staleness contract, it is how it is kept: what
    changes the answer changes the key, so the superseded entry is still sitting there and
    is simply never asked for again. Both halves of that, since they are stamped by
    different things — the window by the room's own stat, the topic by topics_written."""

    def listed(field: str) -> dict:
        return {r["room"]: r[field] for r in store.room_stats(tmp_path)["rooms"]}

    store.append(tmp_path, "aaa", "bot", "one")
    assert listed("last_seq")["aaa"] == 1
    store.append(tmp_path, "aaa", "bot", "two")  # a new (mtime_ns, size)
    assert listed("last_seq")["aaa"] == 2, "the changed room is re-read on the next walk"

    assert listed("topic")["aaa"] is None
    store.note_set(tmp_path, store.TOPIC_NS, "aaa", "what aaa is for")  # bumps topics_written
    assert listed("topic")["aaa"] == "what aaa is for", "a topic is visible immediately"


# ------------------------------------------------------------- the TTL, and its zero


def test_a_zero_ttl_goes_round_the_topic_cache_including_an_entry_already_in_it(
    tmp_path, monkeypatch
):
    """NOTE_STATS_CACHE_SECONDS is read per call, so zero disables an existing entry too —
    documented behaviour, and the reason a reaper's deletion ages out at all. Zero disables
    it by going round the cache rather than by emptying it (nothing is evicted, so putting
    the knob back does not pay for a re-read) and never by asking for the bucket of a
    window that is not open.
    """
    store._topics_memo.cache_clear()
    root = str(tmp_path)
    reads_the_note = store.topic
    gate = _Gated(reads_the_note)  # the real read, counted: this test is about how often
    monkeypatch.setattr(store, "topic", gate)
    store.note_set(tmp_path, store.TOPIC_NS, "aaa", "what aaa is for")

    assert store._cached_topic(root, "aaa", STAMP, NOW) == "what aaa is for"
    assert store._cached_topic(root, "aaa", STAMP, NOW) == "what aaa is for"
    assert gate.calls == 1, "the second read came from the cache"

    store.note_path(tmp_path, store.TOPIC_NS, "aaa").unlink()  # a reaper-style deletion
    with config.override(NOTE_STATS_CACHE_SECONDS=0):
        assert store._cached_topic(root, "aaa", STAMP, NOW) is None
        assert gate.calls == 2, "and it was read, not answered from the entry that is there"
    assert store._cached_topic(root, "aaa", STAMP, NOW) == "what aaa is for"
    assert gate.calls == 2, "the entry survived the zero: nothing was evicted for it"


def test_a_bucket_boundary_can_only_expire_an_entry_sooner_than_its_own_window():
    """The claim store._time_bucket makes, checked rather than argued.

    An entry that carried its own expiry got the whole window measured from its insertion;
    an entry keyed on a bucket gets the tail of the window it landed in. So the boundary can
    only move an expiry earlier, which costs a walk, never later, which would cost the
    published staleness bound (ROOMS_CACHE_SECONDS, NOTE_STATS_CACHE_SECONDS).
    """
    for ttl in (0.5, 3.0, 30.0):
        for start in (0.0, ttl * 0.999, ttl * 7 + 0.25, 1_000_000.0):
            bucket = store._time_bucket(start, ttl)
            expiry = (bucket + 1) * ttl
            assert expiry <= start + ttl, "an entry never outlives its own window"
            assert store._time_bucket(expiry - ttl * 1e-6, ttl) == bucket, "valid until it"
            assert store._time_bucket(expiry, ttl) != bucket, "and a new key at it"


def test_a_disabled_ttl_is_a_bypass_at_the_call_site_and_never_a_bucket():
    """Zero is "no cache", which every caller handles by going round its cache. Asking for
    the bucket of a window that is not open is a bug, and it fails loudly rather than
    quietly handing every entry one shared eternal bucket."""
    with pytest.raises(ZeroDivisionError):
        store._time_bucket(NOW, 0)
