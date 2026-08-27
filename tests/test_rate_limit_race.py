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
    """The crash in #378 was an intermittent KeyError -> 500. The bug only fires while an
    eviction (popitem) is live at the same moment another thread touches the same key, so a
    single repeated key inserts once, fires exactly one eviction, and then len(_buckets) never
    exceeds max_buckets again — popitem never runs for the rest of the run (that was the flaw
    a reviewer caught). Every call here introduces a *distinct* key against a tiny bucket, so
    eviction fires on essentially every call and can land on a key another thread is mid-update.
    """
    max_buckets = 3
    limit._buckets.clear()
    limit._identities.clear()
    for i in range(max_buckets):
        limit._buckets[(f"10.0.0.{i}", "read")] = (1.0, time.monotonic())

    counter = {"n": 0}
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(tid: int):
        for _ in range(300):
            with lock:
                counter["n"] += 1
                ip = f"10.9.{tid}.{counter['n'] % 50}"  # distinct, but bounded so eviction stays live
            try:
                limit.take(_FakeRequest(ip), "read", 60.0, max_buckets=max_buckets)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"{len(errors)} exceptions: {errors[:3]}"
    assert len(limit._buckets) <= max_buckets  # eviction actually ran


def test_take_race_is_closed_by_pop_then_insert():
    """#378 is a race, not a deterministic crash, so the concurrency test above is a smoke
    check. This test pins the *mechanism* against the real `limit.take()`: it wraps
    `_buckets` in a dict that, on the A-key's first insert, evicts that key (and trims to
    capacity) before any subsequent OrderedDict op runs — the exact window the pre-fix
    setitem+move_to_end left open. Against the real `take()`, the shipped pop-then-insert
    path must NOT raise, while a patched pre-fix `take()` (setitem+move_to_end) MUST."""
    import limit as _limit

    def run(real_take):
        _limit._buckets.clear()
        _limit._identities.clear()
        for i in range(3):
            _limit._buckets[(f"seed.{i}", "read")] = (1.0, time.monotonic())

        class Evictor:
            def __init__(self, real):
                self._r = real
                self._a_set = False

            def __getitem__(self, k):
                return self._r[k]

            def __setitem__(self, k, v):
                if k == ("A", "read") and not self._a_set:
                    self._a_set = True
                    self._r[k] = v
                    self._r.pop(("A", "read"), None)
                    while len(self._r) > 3:
                        self._r.popitem(last=False)
                    return
                self._r[k] = v

            def move_to_end(self, k):
                return self._r.move_to_end(k)

            def pop(self, k, default=None):
                return self._r.pop(k, default)

            def popitem(self, last=False):
                return self._r.popitem(last)

            def clear(self):
                self._r.clear()

            def __contains__(self, k):
                return k in self._r

            def __len__(self):
                return len(self._r)

            def get(self, k, default=None):
                return self._r.get(k, default)

        _limit._buckets = Evictor(_limit._buckets)  # type: ignore
        try:
            real_take(_FakeRequest("A"), "read", 60.0, max_buckets=3)
            return None
        except KeyError as exc:
            return exc
        finally:
            _limit._buckets = _limit._buckets._r  # type: ignore

    # Real shipped code must survive the forced interleave.
    fixed_err = run(lambda *a, **k: _limit.take(*a, **k))
    assert fixed_err is None, "shipped pop-then-insert path must NOT raise under the forced interleave"

    # A patched pre-fix path (setitem+move_to_end, the exact bug) must raise under it.
    def pre_fix_take(request, kind, per_min=60.0, burst=None, max_buckets=3):
        ip = request.client.host
        now = time.monotonic()
        _limit._buckets[(ip, kind)] = (1.0, now)
        _limit._buckets.move_to_end((ip, kind))
        while len(_limit._buckets) > max_buckets:
            _limit._buckets.popitem(last=False)
        return 1, 0.0

    buggy_err = run(pre_fix_take)
    assert buggy_err is not None, "pre-fix setitem+move_to_end path SHOULD raise under the forced interleave"


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
