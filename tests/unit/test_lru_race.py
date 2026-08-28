"""Run: uv run --group dev python -m pytest tests

Regression tests for the pop-then-insert LRU race fixes (#378, #376).

`move_to_end` on a module-level OrderedDict shared across Starlette's sync
threadpool is a KeyError waiting to happen: Thread A re-writes an existing key
(leaving it where it was — possibly the front), Thread B's eviction loop runs
`popitem(last=False)` and removes that very key, then Thread A's
`move_to_end(key)` raises. The fix is pop-then-insert, which turns the worst
case into a harmless cache miss instead of an unhandled 500.

The tests below are deterministic, not probabilistic: they drive the exact
interleaving from the issue by injecting a concurrent eviction *inside*
`__setitem__`/`get`, so the old code raises `KeyError` every time and the fixed
code never does.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import limit  # noqa: E402
import store  # noqa: E402


class EvictingDict(OrderedDict):
    """Simulates a concurrent evictor.

    When `armed`, every `__setitem__` (and, if `evict_on_get`, every `get` that
    hits) additionally runs `popitem(last=False)` — "another thread just evicted
    the oldest entry between my write and my move_to_end".
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.armed = False
        self.evict_on_get = False

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if self.armed and len(self) > 1:
            super().popitem(last=False)

    def get(self, key, default=None):
        result = super().get(key, default)
        if self.armed and self.evict_on_get and result is not None and len(self) > 1:
            super().popitem(last=False)
        return result


def _fake_request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


def test_take_does_not_keyerror_when_bucket_is_evicted_mid_call(monkeypatch):
    """#378: a concurrent evictor removing the just-written key must not crash `take()`.

    Old code: `_buckets[k] = v; _buckets.move_to_end(k)`. With the bucket at the
    front, `__setitem__` leaves it there, the injected evictor pops it, and
    `move_to_end` raises KeyError. Fixed code pops first, so the evictor can only
    remove a different key — a harmless cache miss.
    """
    buckets = EvictingDict()
    monkeypatch.setattr(limit, "MAX_IDENTITIES", 10**9)
    monkeypatch.setattr(limit, "_buckets", buckets)

    # Prime the bucket at the FRONT, with a second entry behind it.
    buckets[("1.2.3.4", "read")] = (1.0, 0.0)
    buckets[("5.6.7.8", "read")] = (1.0, 0.0)
    assert next(iter(buckets)) == ("1.2.3.4", "read")  # it is the oldest

    buckets.armed = True  # every subsequent write also evicts the front
    # max_buckets huge so take()'s own eviction loop stays out of the way.
    limit.take(_fake_request("1.2.3.4"), "read", per_min=60, max_buckets=10**9)

    # Reached here without KeyError; the bucket still tracks our IP.
    assert ("1.2.3.4", "read") in buckets


def test_cached_window_hit_path_survives_eviction(tmp_path, monkeypatch):
    """#376 hit path: get() then eviction, then (old) move_to_end must not crash."""
    memo = EvictingDict()
    monkeypatch.setattr(store, "_WINDOW_MEMO_MAX", 10**9)
    monkeypatch.setattr(store, "_window_memo", memo)

    store.append(tmp_path, "rooma", "bot", "hello")
    key = (str(tmp_path), "rooma")

    # Build a controlled memo: `key` is the OLDEST entry, a dummy behind it.
    memo.clear()
    memo[key] = ((0,), ["hello"])
    memo[("__newer__", "__newer__")] = ((9,), ["x"])
    assert next(iter(memo)) == key  # key is the front entry

    memo.armed = True
    memo.evict_on_get = True
    view = store._cached_window(tmp_path, "rooma", (0,))  # hit → get() evicts front

    assert view == ["hello"]  # correct value returned, no KeyError


def test_cached_window_miss_path_survives_eviction(tmp_path, monkeypatch):
    """#376 miss path: __setitem__ then eviction, then (old) move_to_end must not crash."""
    memo = EvictingDict()
    monkeypatch.setattr(store, "_WINDOW_MEMO_MAX", 10**9)
    monkeypatch.setattr(store, "_window_memo", memo)

    store.append(tmp_path, "roomb", "bot", "world")
    key = (str(tmp_path), "roomb")

    # Build a controlled memo: `key` is the OLDEST entry (with a stale stamp so
    # the call takes the miss path), a dummy behind it.
    memo.clear()
    memo[key] = ((0,), ["stale"])
    memo[("__newer__", "__newer__")] = ((9,), ["x"])
    assert next(iter(memo)) == key

    memo.armed = True
    view = store._cached_window(tmp_path, "roomb", (1,))  # miss → __setitem__ evicts front

    assert view == (1, ["bot"])  # room_window result: (last_seq, nicks newest-first)
    assert key in memo


def test_stress_take_never_raises_under_threads(monkeypatch):
    """Belts and braces: hammer take() from many threads with a tiny bound.

    Probabilistic, but the deterministic tests above carry the guarantee.
    """
    monkeypatch.setattr(limit, "MAX_IDENTITIES", 10**9)
    limit._buckets.clear()

    errors: list[BaseException] = []
    barrier = threading.Barrier(16)

    def worker(i: int):
        try:
            barrier.wait()
            for j in range(150):
                ip = f"2001:db8::{i:x}:{j:x}"
                limit.take(_fake_request(ip), "read", per_min=60, max_buckets=2)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"take() raised under concurrency: {errors[0]!r}"
    assert len(limit._buckets) <= 2
