"""Regression tests for _buckets race conditions (Issue #378).

The lock added to _buckets serializes three operations that were racing in production:
1. The read-modify-write of a bucket's balance (two threads both spending the same token)
2. The KeyError from move_to_end on a key another thread just evicted
3. The same pattern in refund()

These tests replace _buckets with a gated OrderedDict that forces specific interleavings.
"""

import threading
import time
from collections import OrderedDict

import limit


class _GatedOrderedDictForKeyError(OrderedDict):
    """An OrderedDict that parks the slow thread after __setitem__ before move_to_end,
    giving the fast thread time to evict the key."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.parked = threading.Event()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if threading.current_thread().name == "slow" and key[0] == "old":
            self.parked.set()  # Signal we've written
            self.gate.wait(timeout=2.0)  # Wait for fast thread to evict


def test_concurrent_take_never_raises_keyerror(monkeypatch):
    """The KeyError path: move_to_end on a key another thread evicted between __setitem__
    and move_to_end.

    Uses a gated OrderedDict where the slow thread parks after __setitem__, giving the
    fast thread (with a DIFFERENT host) time to create a new key that triggers eviction
    of the slow thread's key. On the unfixed base this raises KeyError when slow calls
    move_to_end on the evicted key. With the lock, slow holds it through the whole
    operation and fast waits.
    """
    gated = _GatedOrderedDictForKeyError()

    # Fill to MAX_BUCKETS and add the key slow thread will update
    gated[("old", "read")] = (10.0, 0.0)
    for i in range(20_001):
        gated[(f"filler-{i}", "read")] = (10.0, 0.0)

    monkeypatch.setattr(limit, "_buckets", gated)

    class FakeRequestOld:
        client = type("obj", (), {"host": "old"})()
        scope = {}
        headers = {}

    class FakeRequestNew:
        client = type("obj", (), {"host": "new"})()
        scope = {}
        headers = {}

    results = {}

    def take_slow():
        try:
            results["slow"] = limit.take(FakeRequestOld(), "read", 60)
        except KeyError as e:
            results["slow"] = e

    def take_fast():
        gated.parked.wait(timeout=2.0)
        try:
            # This creates ("new", "read") which forces eviction since we're over MAX_BUCKETS
            results["fast"] = limit.take(FakeRequestNew(), "read", 60)
        except Exception as e:
            results["fast"] = e
        finally:
            gated.gate.set()

    slow_thread = threading.Thread(target=take_slow, name="slow")
    fast_thread = threading.Thread(target=take_fast, name="fast")

    slow_thread.start()
    fast_thread.start()
    slow_thread.join(timeout=3.0)
    fast_thread.join(timeout=3.0)

    assert not isinstance(results.get("slow"), KeyError), (
        f"move_to_end raised KeyError: {results.get('slow')} — "
        "the lock is missing or does not cover the whole section"
    )
    assert isinstance(results.get("slow"), tuple), f"slow: {results.get('slow')}"
    assert isinstance(results.get("fast"), tuple), f"fast: {results.get('fast')}"


class _GatedOrderedDictForBudget(OrderedDict):
    """An OrderedDict that parks thread1 after get(), giving thread2 time to also read
    the same balance before either writes."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.first_read = threading.Event()

    def get(self, key, default=None):
        result = super().get(key, default)
        if threading.current_thread().name == "thread1" and key == ("racer", "read"):
            self.first_read.set()  # Signal we've read
            self.gate.wait(timeout=0.15)  # Wait for thread2 to also read
        return result


def test_concurrent_take_conserves_the_budget(monkeypatch):
    """The lost-update path: two threads read the same balance, both spend a token, both
    write back, one write is lost.

    Uses a gated OrderedDict where thread1 parks after get(), giving thread2 time to also
    call get() and read the same pre-spend balance. On the unfixed base both threads see
    balance=1.0 and both grant (lost update). With the lock, thread2 waits for thread1 to
    complete its entire take() before thread2's get() runs.
    """
    gated = _GatedOrderedDictForBudget()
    gated[("racer", "read")] = (1.0, time.monotonic())

    monkeypatch.setattr(limit, "_buckets", gated)

    class FakeRequest:
        client = type("obj", (), {"host": "racer"})()
        scope = {}
        headers = {}

    results = []

    def take_once():
        left, wait = limit.take(FakeRequest(), "read", 60, burst=1)
        if wait == 0.0:
            results.append(left)

    def take_thread1():
        take_once()

    def take_thread2():
        gated.first_read.wait(timeout=1.0)
        take_once()
        gated.gate.set()

    thread1 = threading.Thread(target=take_thread1, name="thread1")
    thread2 = threading.Thread(target=take_thread2, name="thread2")

    thread1.start()
    thread2.start()
    thread1.join(timeout=1.0)
    thread2.join(timeout=1.0)

    grants = len(results)
    assert grants <= 1, (
        f"granted {grants} tokens from a 1-token bucket with no refill — "
        "the read-modify-write lost an update"
    )
