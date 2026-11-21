"""Run: uv run --group dev python -m pytest tests

Sync handlers (room_say, note_write, rooms, ...) run in Starlette's threadpool, so two
requests from one IP can call limit.take() at the same instant. take()'s read/compute/
write on the module-level _buckets dict has no lock: this pins the one-token case as
observable behavior (how many of two concurrent callers are granted), not as a detail of
_buckets' own implementation.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from types import SimpleNamespace

import limit


def _fake_request(ip: str) -> SimpleNamespace:
    """The one thing take() reads off a request when no ip_header is configured."""
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


class _RendezvousBucket(OrderedDict):
    """get() reads immediately, then holds the value it read until a second caller has
    also read -- so neither call can return (and take() cannot write back) until both
    have observed the same pre-write state. OrderedDict, like the real _buckets, because
    take() also calls move_to_end() and popitem(last=False) on it.

    The hold is a barrier with a timeout, not the coordination mechanism itself: if
    take()'s read-modify-write is serialized (by whatever means -- this test does not
    know or care how), the second call's get() cannot happen until the first has finished
    and released whatever it holds. The first then times out waiting for a second reader
    that will not arrive in time, returns what it already (correctly) read, and finishes;
    the second, once admitted, reads the first's already-written value and its own
    barrier wait breaks immediately (the barrier is already broken) instead of pairing it
    with a stale read. Two callers timing out separately and each returning their own
    honestly-read value is serialized behavior working, not a failure of this helper --
    and it is only ever paid once, by whichever caller is first.
    """

    def __init__(self, barrier: threading.Barrier):
        super().__init__()
        self._barrier = barrier

    def get(self, key, default=None):
        value = super().get(key, default)  # the read happens first, always
        try:
            self._barrier.wait(timeout=0.25)  # then hold it until a second reader catches up
        except threading.BrokenBarrierError:
            pass  # no second reader arrived in time -- return what was actually read
        return value


def test_two_concurrent_takes_on_a_one_token_bucket_grant_exactly_one(monkeypatch) -> None:
    """A bucket holding one token, read by two threads that are forced to see the same
    pre-decrement state before either writes back.

    This is deliberately agnostic to HOW (or whether) take() is made safe: it forces the
    interleaving and asserts only the observable grant/refuse outcome, not any detail of
    _buckets' own implementation.

    Unlocked, both threads read the untouched (1.0, now) starting tuple, both compute
    tokens >= 1.0, and both are granted: two wait==0 results out of a bucket of one, the
    lost update. Serialized, the second thread's get() cannot reach the barrier until the
    first has finished and released whatever serializes them, so it observes the first's
    already-spent bucket instead and is refused.
    """
    barrier = threading.Barrier(2)
    monkeypatch.setattr(limit, "_buckets", _RendezvousBucket(barrier))

    ip = "203.0.113.9"
    # Appended, not indexed: list.append is atomic under the GIL, and which caller finishes
    # first does not matter -- only that, across both, exactly one grant and one refusal
    # come back.
    results: list[tuple[int, float]] = []

    def call() -> None:
        results.append(limit.take(_fake_request(ip), "probe", per_min=1, burst=1))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads), "a take() call never returned"
    assert len(results) == 2, f"expected 2 results, got {results}"

    granted = [wait for _, wait in results if wait == 0.0]
    refused = [wait for _, wait in results if wait > 0.0]
    assert len(granted) == 1 and len(refused) == 1, (
        f"a one-token bucket did not grant exactly one of two concurrent callers: results={results}"
    )


# This race is direction-sensitive -- eviction has to land strictly between the target
# key's insert and its own move_to_end -- unlike the lost-update race above, where either
# resume order produces the same grant/refuse outcome. Reusing _RendezvousBucket's shared
# barrier here made resume order ambiguous and the test flaky, so this uses an explicit
# inserted/resume Event pair instead: the test's own resume.set() drives release on the
# unlocked path (once the real second take() has actually run), and the wrapper's own
# bounded wait is paid only as a hang-guard on the serialized path.
class _EvictionRaceBucket(OrderedDict):
    """__setitem__ inserts normally, then -- only for the one key under test -- signals
    that the insert has landed and pauses (bounded by a hang-guard timeout the test always
    clears itself) before returning control to the caller. That lands take()'s own next
    statement, move_to_end, exactly after whatever happens in the window: a second take()
    call really evicting the same key when unlocked, or nothing at all once the lock has
    serialized the two calls end to end.

    The pause is a bounded wait, not the coordination mechanism itself: if take()'s
    read-modify-write is serialized (by whatever means -- this test does not know or care
    how), the second call cannot even start its own eviction loop until the first has
    released whatever serializes them. The first then times out waiting for a release that
    will not arrive in time, proceeds with the key still its own, and finishes; only then
    does the second call run, evicting a key nothing is still reading. The test's own
    resume.set() -- never the timeout -- is what releases the first call whenever the
    second one legitimately got to run before it, so the timeout is paid only on the
    serialized path, as a hang-guard.
    """

    def __init__(
        self, watch_key: tuple[str, str], inserted: threading.Event, resume: threading.Event
    ):
        super().__init__()
        self._watch_key = watch_key
        self._inserted = inserted
        self._resume = resume

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)  # the write happens first, always
        if key == self._watch_key:
            self._inserted.set()
            self._resume.wait(timeout=0.25)  # hang-guard only: the test always sets this


def test_move_to_end_survives_a_concurrent_eviction_of_the_same_key(monkeypatch) -> None:
    """#378: after take() writes (ip, kind) into _buckets, it calls move_to_end on that
    same key. Unlocked, a second take() (any IP, any kind) can run its own eviction loop
    in between and pop the just-written key as the LRU victim, so move_to_end then raises
    KeyError on a key that was there an instant ago. Serialized, the second call cannot
    even reach its own eviction loop until the first's write, move_to_end and eviction
    have all completed and released whatever serializes them, so there is nothing left to
    evict out from under a move_to_end that has not happened yet.

    Deliberately agnostic to HOW (or whether) take() is made safe: the wrapper only forces
    the interleaving around one dict write, and the test asserts only whether move_to_end
    raised, not on the presence or name of a lock.
    """
    watch_key = ("203.0.113.7", "probe")
    inserted = threading.Event()
    resume = threading.Event()
    monkeypatch.setattr(limit, "_buckets", _EvictionRaceBucket(watch_key, inserted, resume))

    outcome: list[BaseException | None] = []

    def inserter() -> None:
        try:
            limit.take(_fake_request(watch_key[0]), watch_key[1], per_min=1, burst=1, max_buckets=2)
        except BaseException as exc:  # noqa: BLE001 -- the failure under test IS an exception
            outcome.append(exc)
        else:
            outcome.append(None)

    t = threading.Thread(target=inserter)
    t.start()
    assert inserted.wait(timeout=5), "take() never inserted its bucket"
    limit.take(_fake_request("203.0.113.8"), "probe", per_min=1, burst=1, max_buckets=1)
    resume.set()
    t.join(timeout=5)
    assert not t.is_alive(), "take() never returned"
    assert outcome and outcome[0] is None, (
        f"move_to_end raised after a concurrent eviction of the same key: {outcome!r}"
    )
