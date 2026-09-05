"""Run: uv run --group dev python -m pytest tests

The per-IP token bucket's own rules, tested against limit.take and limit.refund
directly. A budget is only a budget if it holds when the callers arrive together:
Starlette runs every sync route in a real thread pool, so "one IP, one bucket" means
one dict entry that several OS threads reach at the same moment.

The concurrency test does not hope for an interleaving. It substitutes a mapping whose
lookup waits for its siblings, which puts every thread past the read before any of them
writes back. That is the interleaving the thread pool produces on its own under load;
pinning it here is what makes the test a statement about the code rather than about the
machine it ran on. Guarded, the threads serialise, the barrier times out once and breaks,
and the arithmetic is the same as it would be one caller at a time.

Parameters are passed explicitly rather than leaning on the shipped defaults, which have
moved with the config: a bucket test asserts arithmetic at numbers it chose.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import limit  # noqa: E402

# One token in the bucket, refilling at one a minute. Chosen so a single grant empties it
# and the refill contributes nothing measurable over the life of the test.
CAP = 1.0
PER_MIN = 1.0
IP_HEADER = "x-test-ip"


class _Headers(dict):
    def get(self, key, default=""):  # Request.headers.get's signature, not dict's
        return dict.get(self, key, default)


class _Request:
    """The two attributes limit.client_ip reads, and nothing else."""

    def __init__(self, ip: str) -> None:
        self.headers = _Headers({IP_HEADER: ip})
        self.client = None


class _SynchronisedBuckets(OrderedDict[tuple[str, str], tuple[float, float]]):
    """An OrderedDict whose get() releases only once every caller has reached it.

    The window this opens is the one the bug lives in: between reading a balance and
    writing the decremented one back. A lock closes it, and then the barrier is never
    satisfied and breaks on the timeout instead, which is the pass condition.
    """

    def __init__(self, parties: int, timeout: float = 0.25) -> None:
        super().__init__()
        self._barrier = threading.Barrier(parties)
        self._timeout = timeout

    def get(self, key, default=None):
        value = super().get(key, default)
        try:
            self._barrier.wait(timeout=self._timeout)
        except threading.BrokenBarrierError:
            pass  # serialised, so the others are not coming: this is the guarded path
        return value


class _ReverseFirstTwoLock:
    """Make the second arriving thread enter the critical section first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arrival_lock = threading.Lock()
        self._first_waiting = threading.Event()
        self._second_entered = threading.Event()
        self._arrivals = 0

    def __enter__(self):
        with self._arrival_lock:
            arrival = self._arrivals
            self._arrivals += 1
        if arrival == 0:
            self._first_waiting.set()
            assert self._second_entered.wait(timeout=1)
        self._lock.acquire()
        if arrival == 1:
            self._second_entered.set()
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()


def _grants(callers: int) -> int:
    """How many of `callers` simultaneous requests from one IP the bucket admitted."""
    saved = limit._buckets
    limit._buckets = _SynchronisedBuckets(callers)
    granted: list[int] = []
    counting = threading.Lock()

    def hit() -> None:
        _, wait = limit.take(
            _Request("198.51.100.7"),
            "write",
            per_min=PER_MIN,
            burst=CAP,
            ip_header=IP_HEADER,
        )
        if wait == 0.0:  # wait of zero is the grant; anything else was refused
            with counting:
                granted.append(1)

    try:
        threads = [threading.Thread(target=hit) for _ in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        limit._buckets = saved
    return len(granted)


def test_a_one_token_bucket_admits_one_caller_however_many_arrive_together():
    """The overdraft this guards against scales with concurrency, so the count matters.

    Unguarded, every thread reads the full bucket and every thread is admitted: four
    callers spend four tokens out of a bucket holding one, and only the last decrement
    survives. That is the whole per-IP budget, bypassable by opening more connections.
    """
    assert _grants(4) == 1, "a bucket holding one token cannot fund four requests"


def test_a_refund_hands_back_exactly_one_token_per_caller():
    """refund() is the same read-modify-write on the same dict, so it races the same way.

    Two refunds racing on a spent bucket must leave two tokens, not one. A lost refund is
    a room-creation charge the caller paid for a room it never created, and it is charged
    against a day-long budget, so it is not repaid by the next refill either.

    The balance is seeded directly rather than by calling take, because take refills from
    the clock and the barrier's own timeout would show up in the arithmetic. refund does
    not refill: it leaves `last` alone on purpose, so two refunds are exactly two tokens.
    """
    saved = limit._buckets
    buckets = _SynchronisedBuckets(2)
    key = ("192.0.2.5", "create")
    buckets[key] = (0.0, time.monotonic())
    limit._buckets = buckets
    request = _Request("192.0.2.5")
    try:
        threads = [
            threading.Thread(
                target=limit.refund,
                args=(request, "create", PER_MIN, 4.0),
                kwargs={"ip_header": IP_HEADER},
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        back, _ = buckets[key]
    finally:
        limit._buckets = saved
    assert back == 2.0, "two refunds must return two tokens, not one"


def test_the_bucket_samples_time_after_entering_the_lock(monkeypatch):
    saved_buckets = limit._buckets
    lock = _ReverseFirstTwoLock()
    key = ("203.0.113.9", "write")
    limit._buckets = OrderedDict({key: (1.0, 0.0)})
    monkeypatch.setattr(limit, "_buckets_lock", lock)
    samples = iter((10.0, 20.0))
    monkeypatch.setattr(limit.time, "monotonic", lambda: next(samples))
    waits: list[float] = []

    def hit() -> None:
        _, wait = limit.take(_Request(key[0]), "write", per_min=1.0, burst=1.0, ip_header=IP_HEADER)
        waits.append(wait)

    try:
        first = threading.Thread(target=hit)
        second = threading.Thread(target=hit)
        first.start()
        assert lock._first_waiting.wait(timeout=1)
        second.start()
        first.join()
        second.join()
        balance, last = limit._buckets[key]
    finally:
        limit._buckets = saved_buckets

    assert sorted(waits) == [0.0, 50.0]
    assert balance == 1 / 6
    assert last == 20.0
