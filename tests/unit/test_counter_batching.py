"""Run: uv run --group dev python -m pytest tests

Every message written to any room bumps the lifetime counters, and until #588 that meant an
exclusive `flock` on one root-level file, held across a read-parse-modify-replace, taken by
writes to rooms that have nothing to do with each other. A store where one room is busy
therefore serialised writes to every *other* room behind it, on a file neither of them reads.

Two rules replace it. A bump whose only delta is `messages` rides in a process-local bucket
instead of paying for a write at all — that is the one counter bumped on every append, and the
one nothing reads for freshness (`app.py`'s ROOMS_STAMP_KEYS leaves it out on purpose). Every
other key marks a structural event another worker's cache stamp is waiting for, so those still
write immediately; and when they cannot get the lock, `LOCK_NB` means they leave their delta
in the same bucket rather than queueing. Four things have to hold, and only the first is about
speed:

- a bump never waits on that lock, whoever is holding it;
- nothing is lost or counted twice on the way through the bucket — not under concurrent
  threads, and not when a replace fails after the batch has been taken out of it;
- a structural bump still persists immediately, and a riding message is bounded — by the
  next structural bump, by BATCH_MESSAGES, and by the snapshot that samples the digest;
- the bucket stays a fixed six keys and empties itself, so a process is not slowly filled
  by the roots it has finished with.
"""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path

import orjson
import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture(autouse=True)
def _clean_pending():
    """`_PENDING` is process-global on purpose, so an undrained delta would otherwise flush
    into whichever test ran next and be counted against its store."""
    import store

    store._PENDING.clear()
    yield
    store._PENDING.clear()


def _persisted(root: Path) -> dict:
    """What is on disk, read the way another process would — never through `counters()`,
    which fills in missing keys and would hide a batch that never landed."""
    import store

    return orjson.loads((root / store.COUNTERS_FILE).read_bytes())


@contextmanager
def _lock_held(root: Path):
    """Hold `.counters.lock` the way another worker would.

    `flock` is per open file description, so a second fd in this process contends exactly as
    a second process does — the same property `tests/unit/test_sharding.py` relies on to say
    `_locked` blocks on itself. That keeps the contention real without a process spawn.
    """
    import store

    root.mkdir(parents=True, exist_ok=True)
    with open(root / (store.COUNTERS_FILE + ".lock"), "a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)


def _drain(root: Path, tries: int = 100) -> None:
    """Flush whatever is pending, the way a test can and a request path deliberately cannot.

    A bump with no deltas adds nothing and takes the same non-blocking path, so this is the
    ordinary flush and not a second implementation of one. Bounded rather than `while`: an
    unflushable root must fail the assertion that follows, never hang the suite.
    """
    import store

    for _ in range(tries):
        if root not in store._PENDING:
            return
        store._bump(root)


# --------------------------------------------------------------------- the uncontended path


def test_a_structural_bump_persists_before_it_returns(tmp_path) -> None:
    """The property most worth not breaking. A create, a reap and a topic write are what
    `_rooms_stamp` compares to decide whether another worker's listing is stale, so those must
    be on disk when the bump returns — batching them would make a second worker's new room
    invisible for a cache window, for reasons no reader could see.
    """
    import store

    store._bump(tmp_path, rooms_created=1)

    assert _persisted(tmp_path)["rooms_created"] == 1
    assert tmp_path not in store._PENDING, "nothing may be left pending when nothing contended"


def test_a_plain_message_rides_in_the_bucket_instead_of_paying_for_a_write(tmp_path) -> None:
    """The change that buys the throughput: the per-append counter does not touch the disk,
    and does not even ask for the lock. Nothing reads `messages` for freshness — it is the one
    key `ROOMS_STAMP_KEYS` leaves out — so the write it would have cost is pure overhead.
    """
    import store

    store._bump(tmp_path, messages=1)

    assert not (tmp_path / store.COUNTERS_FILE).exists(), "a plain message paid for a write"
    assert store._PENDING[tmp_path] == {"messages": 1}


def test_a_structural_bump_flushes_the_messages_riding_with_it(tmp_path) -> None:
    """What bounds the ride in practice. A room create is a structural bump, so the messages
    that accumulated behind it land in the same single write — one replace for all of them,
    which is the whole point.
    """
    import store

    for _ in range(5):
        store._bump(tmp_path, messages=1)
    store._bump(tmp_path, messages=1, rooms_created=1)

    assert _persisted(tmp_path) == {"messages": 6, "rooms_created": 1}
    assert tmp_path not in store._PENDING


def test_the_ride_is_bounded_by_batch_messages(tmp_path) -> None:
    """The backstop for a store that never creates a room: without it a quiet service could
    hold messages in memory indefinitely, and the reaper is no help — it only bumps on a pass
    that actually reaped something.
    """
    import store

    for _ in range(store.BATCH_MESSAGES - 1):
        store._bump(tmp_path, messages=1)
    assert not (tmp_path / store.COUNTERS_FILE).exists(), "flushed before the bound"

    store._bump(tmp_path, messages=1)

    assert _persisted(tmp_path)["messages"] == store.BATCH_MESSAGES
    assert tmp_path not in store._PENDING


def test_a_snapshot_flushes_the_bucket_before_it_samples(tmp_path, monkeypatch) -> None:
    """The digest is the reason these counters exist, and it reads them as a point-in-time
    value. A sample taken over an unflushed bucket reports one window short and the next one
    long — the exact reading the ring exists to get right.

    `SNAPSHOT_EVERY = 0` defeats the throttle, exactly as the sampling test in
    `tests/unit/test_store.py` does: the claim here is about what a due sample contains,
    not about when one is due.
    """
    import store

    store.append(tmp_path, "lobby", "bot", "one")  # creates the room: structural, so it lands
    store.append(tmp_path, "lobby", "bot", "two")
    assert store._PENDING[tmp_path] == {"messages": 1}, "the second message should be riding"

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)  # due now; the throttle is not under test
    store._snapshot(tmp_path)

    sampled = store.snapshots(tmp_path)[-1]["counters"]["messages"]
    assert sampled == 2, "the sample was taken over deltas still sitting in memory"


def test_a_snapshot_is_exact_even_when_the_lock_is_contended(tmp_path, monkeypatch) -> None:
    """The uncontended snapshot test above is not enough: `_snapshot` flushes through `_bump`,
    and if that flush were allowed to decline a busy lock the sample would silently record the
    figure the ring exists to get right — one window short, the next long — while the process
    stayed healthy. The flush has no deltas of its own, so it is not a message bump and waits.
    """
    import store

    store.append(tmp_path, "lobby", "bot", "one")
    store.append(tmp_path, "lobby", "bot", "two")
    assert store._PENDING[tmp_path] == {"messages": 1}, "the second message should be riding"
    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    sampled = []

    def sample() -> None:
        store._snapshot(tmp_path)
        sampled.append(store.snapshots(tmp_path)[-1]["counters"]["messages"])

    with _lock_held(tmp_path):
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        assert not sampler.join(0.25) and not sampled, "it sampled over an unflushed bucket"

    sampler.join(timeout=10)
    assert sampled == [2], "the sample missed a delta that was still in memory"


def test_a_bump_that_cannot_write_at_all_still_does_not_raise(tmp_path) -> None:
    """The contract this function has always had: the caller's write already succeeded, so a
    counter that cannot be written must never turn that success into an error. The new code
    swallows the same `OSError` in the same place — and keeps the delta rather than dropping
    it on the floor, which the old code had no way to do.
    """
    import store

    (tmp_path / "wall").write_text("not a directory")
    root = tmp_path / "wall" / "root"  # every path under it is NotADirectoryError

    store._bump(root, messages=1)

    assert store._PENDING[root]["messages"] == 1


# ------------------------------------------------------------------------- under contention


def test_a_message_flush_does_not_wait_for_a_held_lock(tmp_path) -> None:
    """The bug itself, on the only path still allowed to decline the lock. Under a watchdog
    rather than a stopwatch: the claim is "this returns without the holder releasing", which a
    join timeout states exactly and a wall-clock threshold only approximates — and only the
    latter fails on a loaded CI runner.
    """
    import store

    done = threading.Event()

    def bump() -> None:
        for _ in range(store.BATCH_MESSAGES):  # the last one reaches the bound and the lock
            store._bump(tmp_path, messages=1)
        done.set()

    with _lock_held(tmp_path):
        writer = threading.Thread(target=bump, daemon=True)
        writer.start()
        writer.join(timeout=10)

        assert done.is_set(), "the bump is still waiting on a lock it must never wait on"
        assert not (tmp_path / store.COUNTERS_FILE).exists(), "it wrote under another holder"

    assert store._PENDING[tmp_path] == {"messages": store.BATCH_MESSAGES}, "deltas were dropped"


def test_a_structural_bump_waits_for_the_lock_rather_than_deferring(tmp_path) -> None:
    """The ordering contract the `/rooms` cache stamp rests on, and the one thing the
    non-blocking path must not be allowed to break.

    A structural counter is what another worker compares to decide its cached listing is
    stale. Deferring one means a second worker keeps serving a listing that predates the room
    it should describe, for as long as this process takes to flush — unbounded if it goes
    quiet. So a structural bump waits for the lock and is on disk when it returns, exactly as
    it was before batching. Waiting is safe because `.counters.lock` is a leaf: nothing is
    held while waiting for it and it takes no other lock.
    """
    import store

    done = threading.Event()

    def bump() -> None:
        store._bump(tmp_path, rooms_created=1)
        done.set()

    with _lock_held(tmp_path):
        writer = threading.Thread(target=bump, daemon=True)
        writer.start()
        # A negative check, so it cannot fail spuriously on a slow runner: completing here
        # would mean the flock was granted while another holder had it.
        assert not done.wait(0.25), "a structural bump returned without persisting"
        assert not (tmp_path / store.COUNTERS_FILE).exists()

    writer.join(timeout=10)
    assert done.is_set(), "the bump never completed once the lock was free"
    assert _persisted(tmp_path)["rooms_created"] == 1
    assert tmp_path not in store._PENDING


def test_deltas_accumulate_while_the_lock_is_held(tmp_path) -> None:
    """What the writers that could not get in leave behind: one bucket that adds up, not a
    queue and not a line per call. The size of the state is the point — six keys and an int
    each, whatever the write rate.
    """
    import store

    with _lock_held(tmp_path):
        for _ in range(5):
            store._bump(tmp_path, messages=1)

        assert store._PENDING[tmp_path] == {"messages": 5}
        assert not (tmp_path / store.COUNTERS_FILE).exists()


def test_the_first_bump_after_contention_flushes_the_backlog_exactly_once(tmp_path) -> None:
    """The whole batch, and only once. An off-by-one here is a counter that drifts under
    exactly the load it is meant to survive, and these values are compared by equality on the
    `/rooms` cache stamp, so a double-count is as wrong as a loss.
    """
    import store

    with _lock_held(tmp_path):
        for _ in range(store.BATCH_MESSAGES):  # the bound is reached, but the lock is busy
            store._bump(tmp_path, messages=1)
        assert not (tmp_path / store.COUNTERS_FILE).exists()

    store._bump(tmp_path, messages=1)

    assert _persisted(tmp_path)["messages"] == store.BATCH_MESSAGES + 1
    assert tmp_path not in store._PENDING
    _drain(tmp_path)
    assert _persisted(tmp_path)["messages"] == store.BATCH_MESSAGES + 1, "applied twice"


def test_a_batch_keeps_its_keys_apart(tmp_path) -> None:
    """Batching adds per key, never per call: three bumps naming different counters must land
    as three counters, and a key nobody bumped must not be invented.
    """
    import store

    store._bump(tmp_path, messages=1)
    store._bump(tmp_path, messages=1, rooms_created=1)
    store._bump(tmp_path, notes_written=2)

    _drain(tmp_path)

    assert store.counters(tmp_path) == {
        "messages": 2,
        "rooms_created": 1,
        "reaped_idle": 0,
        "reaped_stillborn": 0,
        "notes_written": 2,
        "topics_written": 0,
    }


# ------------------------------------------------------------------------------- the losses


def test_a_failed_replace_hands_the_batch_back_and_it_lands_once(tmp_path, monkeypatch) -> None:
    """The window a batch could vanish in: it is out of the bucket, the lock is held, and the
    replace fails. Best effort covers *not raising*; it does not license throwing away deltas
    that a retry would have persisted, so they go back and the next bump takes them.
    """
    import store

    store._bump(tmp_path, rooms_created=1)  # a good file, which the failure must leave alone
    calls = []

    def failing_replace(path: Path, data: bytes, fsync: bool = False) -> None:
        calls.append(path)
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "_replace", failing_replace)
    store._bump(tmp_path, rooms_created=5)
    monkeypatch.undo()

    assert calls, "the failure has to happen inside the lock, not before it"
    assert _persisted(tmp_path)["rooms_created"] == 1, "a failed replace must not move it"
    assert store._PENDING[tmp_path] == {"rooms_created": 5}, "and must give the batch back"

    _drain(tmp_path)

    assert _persisted(tmp_path)["rooms_created"] == 6, "the recovered batch lands once"
    assert tmp_path not in store._PENDING


def test_concurrent_threads_neither_lose_nor_double_count(tmp_path) -> None:
    """The real shape of the contention: sync handlers overlapping in one worker's threadpool,
    all bumping the same counter. Every delta has to be either persisted or still pending —
    there is no third place for one to be.
    """
    import store

    threads, each = 8, 200

    def writer() -> None:
        for _ in range(each):
            store._bump(tmp_path, messages=1, notes_written=1)

    workers = [threading.Thread(target=writer, daemon=True) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60)
    assert not any(w.is_alive() for w in workers), "a writer is stuck on the counter lock"

    _drain(tmp_path)

    counted = store.counters(tmp_path)
    assert counted["messages"] == threads * each
    assert counted["notes_written"] == threads * each
    assert tmp_path not in store._PENDING


# ---------------------------------------------------------------------------- the state kept


def test_a_drained_root_leaves_no_state_behind(tmp_path) -> None:
    """One entry per root would otherwise outlive every root a process ever touched — which
    is a test suite with a `tmp_path` per test, or an application pointed at more than one
    store. A drained bucket is removed, not emptied and kept.
    """
    import store

    for i in range(5):
        root = tmp_path / f"root{i}"
        root.mkdir()
        store._bump(root, rooms_created=1)
        assert _persisted(root)["rooms_created"] == 1

    assert store._PENDING == {}


def test_a_corrupt_counter_file_is_still_ignored_rather_than_trusted(tmp_path) -> None:
    """Unchanged from before batching: a counter file that is not an object of ints is a
    diagnostic that got corrupted, never authority. The batch has to land on top of zero
    rather than on top of whatever `[]` would have parsed to.
    """
    import store

    (tmp_path / store.COUNTERS_FILE).write_text("[]")

    store._bump(tmp_path, messages=1, rooms_created=1)

    assert store.counters(tmp_path) == dict.fromkeys(store.COUNTER_KEYS, 0) | {
        "messages": 1,
        "rooms_created": 1,
    }
