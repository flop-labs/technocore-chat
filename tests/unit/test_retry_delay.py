"""A retry delay must survive the integer-second HTTP renderers without going early."""

from collections import OrderedDict
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import limit


@pytest.mark.parametrize(
    ("kind", "per_min", "burst", "tokens", "expected"),
    [
        ("read", 2, None, 0.25, 23),  # 22.5 rounds DOWN to 22 with round().
        ("write", 2, None, 0.02, 30),  # 29.4 is also too early at 29.
        ("create", 7 / 1440.0, 7, 0.25, 9258),  # Daily budget, fractional refill.
        ("read", 2, None, 0.0, 30),  # An exact integer must not gain a second.
        ("write", 60, None, 0.5, 1),  # A sub-second refusal must stay positive.
    ],
)
def test_take_returns_a_safe_whole_second_retry_delay(
    monkeypatch, kind, per_min, burst, tokens, expected
):
    now = SimpleNamespace(value=1000.0)
    # Replace only this module's clock, not asyncio's shared time.monotonic.
    monkeypatch.setattr(limit, "time", SimpleNamespace(monotonic=lambda: now.value))
    monkeypatch.setattr(limit, "_buckets", OrderedDict())
    monkeypatch.setattr(limit, "_identities", set())
    monkeypatch.setattr(limit, "_requests", {"rate_limited": 0})
    ip = "192.0.2.1"
    request = Request({"type": "http", "headers": [], "client": (ip, 1234)})
    limit._buckets[(ip, kind)] = (tokens, now.value)

    left, retry = limit.take(request, kind, per_min, burst)
    assert left == 0
    assert retry == expected
    assert isinstance(retry, int)
    # Rounding the advice must not round up the balance or spend a refused token.
    assert limit._buckets[(ip, kind)] == (tokens, now.value)

    now.value += retry
    _, next_retry = limit.take(request, kind, per_min, burst)
    assert next_retry == 0
    remaining, _ = limit._buckets[(ip, kind)]
    assert remaining == pytest.approx(tokens + retry * per_min / 60.0 - 1.0)
