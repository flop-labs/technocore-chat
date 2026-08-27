"""Run: uv run --group dev python -m pytest tests"""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


def _request(host):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=host))


def test_take_conserves_the_budget_under_contention(monkeypatch):
    """#378's real stake: _buckets is state, not a cache. The pop-shaped fix for the
    move_to_end KeyError opened a gap where a concurrent take() finds the key absent
    and mints a full budget — a rate-limit reset, not a cache miss. Under the lock the
    whole read-refill-spend is atomic, so a burst of N callers against a cap of 5 gets
    exactly 5 grants, however they interleave. (A threadless hostile-dict repro is not
    possible here: the lock excludes the interleave it would have to fake, so real
    threads are the honest test.)"""
    import limit

    monkeypatch.setattr(limit, "_buckets", limit.OrderedDict())
    barrier = threading.Barrier(25)

    def one():
        barrier.wait()
        # per_min=1 keeps refill negligible over the test's lifetime (1 token/min)
        return limit.take(_request("203.0.113.9"), "read", 1, burst=5)

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = [f.result() for f in [pool.submit(one) for _ in range(25)]]

    grants = [r for r in results if r[1] == 0.0]
    refusals = [r for r in results if r[1] > 0.0]
    assert len(grants) == 5, f"budget minted under contention: {len(grants)} grants"
    assert len(refusals) == 20
    assert all(w > 0.0 for _, w in refusals)


def test_take_survives_the_eviction_hammer(monkeypatch):
    """The original #378 crash: one worker's popitem landing between another's bucket
    assignment and its move_to_end. max_buckets=1 with two alternating IPs makes every
    call an eviction of the other's entry, so eight threads drive that interleave
    thousands of times; under the lock none of them can raise."""
    import limit

    monkeypatch.setattr(limit, "_buckets", limit.OrderedDict())

    def hammer(host):
        for _ in range(250):
            limit.take(_request(host), "read", 120, max_buckets=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(hammer, f"198.51.100.{i % 2}") for i in range(8)]
        for f in futures:
            f.result()  # re-raises the KeyError the unlocked shape could hit

    assert len(limit._buckets) <= 1  # the bound held throughout


def test_take_never_exposes_an_absent_bucket_midflight(monkeypatch):
    """The deterministic version of the budget-mint gap (review of the first #378
    attempt): any shape that removes the key mid-flight lets a caller landing in the
    gap read an absent bucket and mint a full budget. The dict below arms that gap —
    a second take() for the same IP fires from inside pop() — so a shape that pops
    fails here with an extra grant, while a shape that never uncouples read from
    write leaves the hook cold and the budget intact. (Not a reentrant call under
    the lock: the fixed take() never calls pop, so the hook cannot fire.)"""
    from collections import OrderedDict

    import limit

    inner = []

    class ArmsTheGap(OrderedDict):
        def pop(self, key, default=None):
            value = super().pop(key, default)
            if value is not None and not inner:
                inner.append(limit.take(_request("203.0.113.7"), "read", 1, burst=1))
            return value

    monkeypatch.setattr(limit, "_buckets", ArmsTheGap())

    first = limit.take(_request("203.0.113.7"), "read", 1, burst=1)
    second = limit.take(_request("203.0.113.7"), "read", 1, burst=1)

    grants = [r for r in [first, second, *inner] if r[1] == 0.0]
    assert len(grants) == 1, f"an absent-key gap minted budget: {len(grants)} grants"


def test_refund_serialises_with_take(monkeypatch):
    """A refund is the other read-modify-write of _buckets, so it must share take's
    lock. Without it, a take landing after refund reads but before it writes is erased:
    four tokens become three, then the stale refund writes five instead of four. The
    hostile dict drives that interleave inline only when refund has left the lock open;
    the final ordinary take makes the extra grant observable without timing or threads."""
    from collections import OrderedDict

    import limit

    request = _request("203.0.113.11")
    key = ("203.0.113.11", "create")
    inner = []

    class TakeInsideUnlockedRefund(OrderedDict):
        armed = False

        def get(self, key, default=None):
            value = super().get(key, default)
            if self.armed and not inner and not limit._buckets_lock.locked():
                inner.append(limit.take(request, "create", 1, burst=5))
            return value

    buckets = TakeInsideUnlockedRefund({key: (4.0, limit.time.monotonic())})
    buckets.armed = True
    monkeypatch.setattr(limit, "_buckets", buckets)

    limit.refund(request, "create", 1, burst=5)
    after = limit.take(request, "create", 1, burst=5)

    grants = [result for result in [*inner, after] if result[1] == 0.0]
    assert len(grants) == 1, f"refund erased a concurrent spend: {len(grants)} grants"
    assert 3.9 < buckets[key][0] <= 4.0  # refund(+1) and one take(-1), plus tiny refill


def test_take_samples_the_clock_inside_the_lock(monkeypatch):
    """Review on #420: with the sample outside the lock, two callers can sample and
    acquire in opposite orders, and the stale `now` computes a negative refill against
    the newer `last` — refusing an available bucket with a false wait and rewinding
    `last`. The hostile lock below runs a rival take() to completion from inside the
    first caller's acquire, and the scripted clock hands out strictly increasing
    samples in call order; sampled inside the lock, sample order therefore matches
    mutation order and both callers are granted with `last` monotone."""
    import limit

    request = _request("203.0.113.21")
    key = ("203.0.113.21", "read")
    samples = iter([1.0, 10.0, 20.0, 30.0])
    monkeypatch.setattr(limit.time, "monotonic", lambda: next(samples))

    inner = []

    class FiresRivalOnAcquire:
        fired = False

        def __enter__(self):
            if not FiresRivalOnAcquire.fired:
                FiresRivalOnAcquire.fired = True
                inner.append(limit.take(request, "read", 60, burst=5))
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(limit, "_buckets_lock", FiresRivalOnAcquire())
    monkeypatch.setattr(limit, "_buckets", limit.OrderedDict())

    outer = limit.take(request, "read", 60, burst=5)

    assert inner and inner[0][1] == 0.0, "the rival's grant is the setup, not the test"
    assert outer[1] == 0.0, f"an available bucket was refused (wait={outer[1]})"
    tokens, last = limit._buckets[key]
    assert tokens >= 0.0, f"tokens went negative: {tokens}"
    assert last == 10.0, f"last regressed or froze: {last}"
