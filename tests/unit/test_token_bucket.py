"""Run: uv run --group dev python -m pytest tests

`take()` and `refund()`'s own rules, tested against `limit._buckets` directly. Every
write lane reaches the token bucket from a threadpool — the GET lanes are sync
endpoints, the POST goes through run_in_threadpool — so a burst of concurrent callers
on one (ip, kind) is not a hypothetical, it is the ordinary shape of load. What matters
is not the verdict on one call but the total admitted across every caller that raced
the same bucket: a lock that is missing is invisible in a single-threaded test and only
shows up as an over-admitted budget under a real burst.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import limit  # noqa: E402


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """The only two things client_ip() reads off a request: .headers (for the
    proxy-header check, unused with the default ip_header="") and .client.host."""

    def __init__(self, host: str = "203.0.113.5") -> None:
        self.headers: dict[str, str] = {}
        self.client = _FakeClient(host)


REQUEST = _FakeRequest()


def _run(threads: list[threading.Thread]) -> None:
    """Every thread started, then joined, at the fine-grained switch interval the
    dupe-ring concurrency test already establishes as what makes this codebase's races
    reproducible rather than merely possible."""
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(switch)


def test_concurrent_take_never_admits_more_than_the_burst() -> None:
    """Twenty threads hammer take() on the same (ip, kind) far past the burst. Unguarded,
    a burst of callers can each read the same un-decremented balance and each grant
    independently — the over-admission scales with how many raced the same snapshot, not
    with a fixed fraction of a token (limit.py's own comment on the un-locked _dupes
    case argues the latter, which does not hold here). `per_min` is set low enough that
    refill during the run cannot itself explain an extra grant.
    """
    limit._buckets.clear()
    key = (REQUEST.client.host, "burst-test")
    per_min, burst = 1e-9, 50
    threads_n, calls_per_thread = 20, 50
    errors: list[BaseException] = []
    granted = itertools.count()

    def hammer() -> None:
        try:
            for _ in range(calls_per_thread):
                _, wait = limit.take(REQUEST, "burst-test", per_min, burst=burst)
                if wait == 0.0:
                    next(granted)
        except BaseException as exc:  # noqa: BLE001 - the exception IS what this asserts on
            errors.append(exc)

    _run([threading.Thread(target=hammer) for _ in range(threads_n)])

    assert not errors, [repr(exc) for exc in errors[:3]]
    admitted = next(granted)
    assert admitted <= burst, f"admitted {admitted} calls against a burst of {burst}"
    assert len(limit._buckets) >= 1 and key in limit._buckets
    limit._buckets.clear()


def test_concurrent_take_and_refund_never_lose_an_update() -> None:
    """Half the threads spend tokens, half refund them, on the same (ip, kind), starting
    and ending far from either the empty or the full boundary so every call is a plain
    read-modify-write with a known effect. Locked, the net change has to equal
    (refunds - takes) exactly, regardless of interleaving order — float addition and
    subtraction by 1.0 is exact at these magnitudes. A lost update from either side
    racing the other shows up as a final balance that does not match.
    """
    limit._buckets.clear()
    key = (REQUEST.client.host, "take-refund-test")
    per_min, burst = 1e-9, 100_000.0
    start = 50_000.0
    # `last` set to now, not 0.0: a stale `last` would let the very first call's own
    # refill term (now - last) span real elapsed time since the epoch, adding a genuine
    # if tiny balance the assertion below would then wrongly read as a lost update.
    limit._buckets[key] = (start, time.monotonic())

    threads_n, calls_per_thread = 10, 200  # 2000 takes, 2000 refunds
    errors: list[BaseException] = []

    def spend() -> None:
        try:
            for _ in range(calls_per_thread):
                limit.take(REQUEST, "take-refund-test", per_min, burst=burst)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def give_back() -> None:
        try:
            for _ in range(calls_per_thread):
                limit.refund(REQUEST, "take-refund-test", per_min, burst=burst)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=spend) for _ in range(threads_n)] + [
        threading.Thread(target=give_back) for _ in range(threads_n)
    ]
    _run(threads)

    assert not errors, [repr(exc) for exc in errors[:3]]
    final_tokens, _ = limit._buckets[key]
    # threads_n * calls_per_thread takes and the same number of refunds started and
    # ended at the same point, net zero, if — and only if — no update was lost.
    assert final_tokens == start, f"expected {start}, got {final_tokens} — an update was lost"
    limit._buckets.clear()
