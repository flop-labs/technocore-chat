"""Run: uv run --group dev python -m pytest tests

Every message written to any room bumps the lifetime counters, and until #588 that meant an
exclusive `flock` on one root-level file, held across a read-parse-modify-replace, taken by
writes to rooms that have nothing to do with each other. A store where one room is busy
therefore serialised writes to every *other* room behind it, on a file neither of them reads.

The lock is `LOCK_NB` now: a writer that finds it held adds its delta to a process-local
bucket and returns, and the next writer that does get the lock persists the accumulated batch
in the same single replace one bump always cost. Four things have to hold, and only the first
is about speed:

- a bump never waits on that lock, whoever is holding it;
- nothing is lost or counted twice on the way through the bucket — not under concurrent
  threads, and not when a replace fails after the batch has been taken out of it;
- a quiet store still persists every bump immediately, so `/stats` on an idle service is
  exactly as fresh as it was before;
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


def test_an_uncontended_bump_persists_before_it_returns(tmp_path) -> None:
    """The property most worth not breaking. Batching is for the contended case only: on a
    quiet store — one worker, no overlap — every bump must still be on disk when it returns,
    or `/stats` and the `/rooms` cache stamp start reporting a store that is behind for
    reasons no reader can see.
    """
    import store

    store._bump(tmp_path, messages=1)

    assert _persisted(tmp_path)["messages"] == 1
    assert tmp_path not in store._PENDING, "nothing may be left pending when nothing contended"


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


def test_a_held_lock_does_not_make_a_writer_wait(tmp_path) -> None:
    """The bug itself. Under a watchdog rather than a stopwatch: the claim is "this returns
    without the holder releasing", which a join timeout states exactly and a wall-clock
    threshold only approximates — and only the latter fails on a loaded CI runner.
    """
    import store

    done = threading.Event()

    def bump() -> None:
        store._bump(tmp_path, messages=1)
        done.set()

    with _lock_held(tmp_path):
        writer = threading.Thread(target=bump, daemon=True)
        writer.start()
        writer.join(timeout=10)

        assert done.is_set(), "the bump is still waiting on a lock it must never wait on"
        assert not (tmp_path / store.COUNTERS_FILE).exists(), "it wrote under another holder"

    assert store._PENDING[tmp_path]["messages"] == 1, "and the delta is kept, not dropped"


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
        store._bump(tmp_path, messages=2)
        store._bump(tmp_path, messages=3)

    store._bump(tmp_path, messages=1)

    assert _persisted(tmp_path)["messages"] == 6
    assert tmp_path not in store._PENDING
    _drain(tmp_path)
    assert _persisted(tmp_path)["messages"] == 6, "a drained batch must not be applied again"


def test_a_batch_keeps_its_keys_apart(tmp_path) -> None:
    """Batching adds per key, never per call: three bumps naming different counters must land
    as three counters, and a key nobody bumped must not be invented.
    """
    import store

    with _lock_held(tmp_path):
        store._bump(tmp_path, messages=1, rooms_created=1)
        store._bump(tmp_path, notes_written=2)
        store._bump(tmp_path, messages=1)

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

    store._bump(tmp_path, messages=1)  # a good file, which the failure must leave alone
    calls = []

    def failing_replace(path: Path, data: bytes, fsync: bool = False) -> None:
        calls.append(path)
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "_replace", failing_replace)
    store._bump(tmp_path, messages=5)
    monkeypatch.undo()

    assert calls, "the failure has to happen inside the lock, not before it"
    assert _persisted(tmp_path)["messages"] == 1, "a failed replace must not move the file"
    assert store._PENDING[tmp_path] == {"messages": 5}, "and must give the batch back"

    _drain(tmp_path)

    assert _persisted(tmp_path)["messages"] == 6, "the recovered batch lands exactly once"
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
        store._bump(root, messages=1)
        assert _persisted(root)["messages"] == 1

    assert store._PENDING == {}


def test_a_corrupt_counter_file_is_still_ignored_rather_than_trusted(tmp_path) -> None:
    """Unchanged from before batching: a counter file that is not an object of ints is a
    diagnostic that got corrupted, never authority. The batch has to land on top of zero
    rather than on top of whatever `[]` would have parsed to.
    """
    import store

    (tmp_path / store.COUNTERS_FILE).write_text("[]")

    store._bump(tmp_path, messages=1)

    assert store.counters(tmp_path) == dict.fromkeys(store.COUNTER_KEYS, 0) | {"messages": 1}
    assert store.counters(tmp_path)["messages"] == 1
