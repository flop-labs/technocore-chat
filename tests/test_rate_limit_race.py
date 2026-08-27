"""Regression tests for the _buckets OrderedDict race in limit.take() (#378).

take() runs in Starlette's threadpool, so concurrent calls race on the module-level
OrderedDict. The old setitem+move_to_end left a window where another thread's
popitem(last=False) could evict the key, and move_to_end then raised KeyError -> 500.
The fix is pop-then-insert; these tests pin both the absence of the crash and the
LRU ordering the eviction still relies on.
"""

from __future__ import annotations

import threading
import time

import limit


def _seed_bucket(n: int = 5):
    """Fill the bucket with n keys using a small max_buckets so eviction fires."""
    limit._buckets.clear()
    limit._identities.clear()
    for i in range(n):
        limit._buckets[(f"10.0.0.{i}", "read")] = (1.0, time.monotonic())


class _Client:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    """Minimal stand-in for a Starlette Request: only client_ip() reads client.host."""

    def __init__(self, host: str):
        self.client = _Client(host)
        self.headers: dict[str, str] = {}


def test_take_does_not_raise_keyerror_under_concurrent_calls():
    """The crash in #378 was an intermittent KeyError -> 500. Hammer take() from many
    threads against a small bucket (so popitem fires) and assert no call raises."""
    _seed_bucket(4)
    errors: list[BaseException] = []

    def worker():
        for _ in range(200):
            try:
                limit.take(_FakeRequest("10.9.9.9"), "read", 60.0, max_buckets=4)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"{len(errors)} exceptions: {errors[:3]}"


def test_take_lru_ordering_preserved_after_pop_insert():
    """pop-then-insert must keep the LRU semantics: a re-taken key moves to the most
    recent end, and the oldest untouched key is the one evicted."""
    _seed_bucket(3)  # bucket holds .0 .1 .2 in insertion order
    # Touch .2 so it becomes most-recent (pop+insert moves it to the end).
    limit.take(_FakeRequest("10.0.0.2"), "read", 60.0, max_buckets=3)
    # A brand-new key should evict the *oldest* (.0), not the one just touched.
    limit.take(_FakeRequest("10.0.0.99"), "read", 60.0, max_buckets=3)
    assert ("10.0.0.0", "read") not in limit._buckets
    assert ("10.0.0.99", "read") in limit._buckets
    assert ("10.0.0.2", "read") in limit._buckets  # preserved, not evicted
