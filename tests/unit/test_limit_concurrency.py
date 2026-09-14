"""Regression tests for _buckets race conditions (Issue #378).

The lock added to _buckets serializes three operations that were racing in production:
1. The read-modify-write of a bucket's balance (two threads both spending the same token)
2. The KeyError from move_to_end on a key another thread just evicted
3. The same pattern in refund()

This file uses a gated OrderedDict to force the interleavings deterministically, matching
the technique already used in tests/unit/test_memo_caches.py for _Gated.
"""

import threading
import time
from collections import OrderedDict

import limit


class _GatedOrderedDict(OrderedDict):
    """An OrderedDict that parks one thread after get() until another thread signals it.

    Used to force the exact interleaving that reproduces issue #378: thread A reads a
    bucket, thread B evicts it, thread A tries to move_to_end the now-absent key.
    """

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.parked = threading.Event()

    def get(self, key, default=None):
        result = super().get(key, default)
        if threading.current_thread().name == "parked":
            self.parked.set()
            self.gate.wait(timeout=2.0)
        return result


def test_concurrent_take_never_raises_keyerror(monkeypatch):
    """The KeyError path: move_to_end on a key another thread evicted between __setitem__
    and move_to_end. Needs MAX_BUCKETS exceeded so popitem runs, and an existing key so
    __setitem__ leaves it where it was (a new key goes to the end and cannot be evicted
    before move_to_end runs).

    Without the lock this raises KeyError in the parked thread. With it, both succeed.
    """
    gated = _GatedOrderedDict()
    gated[("old", "read")] = (10.0, 0.0)
    for i in range(20_001):
        gated[(f"filler-{i}", "read")] = (10.0, 0.0)

    monkeypatch.setattr(limit, "_buckets", gated)

    class FakeRequest:
        client = type("obj", (), {"host": "old"})()
        scope = {}
        headers = {}

    results = {}

    def take_parked():
        try:
            results["parked"] = limit.take(FakeRequest(), "read", 60)
        except KeyError as e:
            results["parked"] = e

    def take_evictor():
        if gated.parked.wait(timeout=2.0):
            results["evictor"] = limit.take(FakeRequest(), "read", 60)
            gated.gate.set()
        else:
            results["evictor"] = "timeout"

    parked_thread = threading.Thread(target=take_parked, name="parked")
    evictor_thread = threading.Thread(target=take_evictor, name="evictor")

    parked_thread.start()
    evictor_thread.start()
    parked_thread.join(timeout=3.0)
    evictor_thread.join(timeout=3.0)

    assert not isinstance(results.get("parked"), KeyError), (
        "move_to_end raised KeyError — the lock is missing or does not cover the whole section"
    )
    assert isinstance(results.get("parked"), tuple), f"unexpected result: {results.get('parked')}"
    assert isinstance(results.get("evictor"), tuple), f"evictor result: {results.get('evictor')}"


def test_concurrent_take_conserves_the_budget(monkeypatch):
    """The lost-update path: two threads read the same balance, both spend a token, both
    write back, one write is lost. The bucket grants more than its capacity.

    Without the lock this fails at default switchinterval on the test matrix. With it,
    the sum of grants never exceeds the bucket's balance plus the refill that happened
    during the test.
    """
    limit._buckets.clear()

    class FakeRequest:
        client = type("obj", (), {"host": "racer"})()
        scope = {}
        headers = {}

    cap = 10
    results = []

    def hammer():
        for _ in range(100):
            left, wait = limit.take(FakeRequest(), "read", cap * 60, burst=cap)
            if wait == 0.0:
                results.append(left)

    started = time.monotonic()
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started

    grants = len(results)
    refilled = elapsed * cap
    assert grants <= cap + refilled, (
        f"granted {grants} from a {cap}-token bucket with {refilled:.1f} refilled — "
        "the read-modify-write is unguarded"
    )