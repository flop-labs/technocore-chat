"""Run: uv run --group dev python -m pytest tests/unit/test_bench_concurrency.py

bench/concurrency.py reports one integer, the tokens granted past the bucket's contents, and that
integer is only worth reading if nothing but a lost update can move it. Two other things could: a
ceiling the server is not actually using, and tokens a slow burst really earned. These pin that
neither gets through.
"""

import importlib.util
import io
import json
import pathlib
import time
import urllib.error

import pytest

BENCH = pathlib.Path(__file__).resolve().parents[2] / "bench" / "concurrency.py"


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("bench_concurrency", BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    module.limit._buckets.clear()


def _config_says(monkeypatch, bench, body: bytes) -> None:
    monkeypatch.setattr(bench.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(body))


def test_a_cap_the_server_does_not_report_refuses_the_run(monkeypatch, bench):
    """A server at 100 told --cap 50 grants 80 and would print an excess of 30, with nothing raced."""
    _config_says(monkeypatch, bench, json.dumps({"settings": {"rate_write": 100}}).encode())
    with pytest.raises(SystemExit, match="rate_write=100"):
        bench._require_served_cap(8099, 50)


def test_the_cap_the_server_reports_is_allowed_through(monkeypatch, bench):
    _config_says(monkeypatch, bench, json.dumps({"settings": {"rate_write": 100}}).encode())
    bench._require_served_cap(8099, 100)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"settings": null}'], ids=str)
def test_a_config_with_no_readable_cap_refuses_the_run(monkeypatch, bench, body):
    _config_says(monkeypatch, bench, body)
    with pytest.raises(SystemExit, match="could not read rate_write"):
        bench._require_served_cap(8099, 100)


def test_a_server_that_cannot_be_reached_refuses_the_run(monkeypatch, bench):
    def down(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(bench.urllib.request, "urlopen", down)
    with pytest.raises(SystemExit, match="could not read rate_write"):
        bench._require_served_cap(8099, 100)


def test_a_slow_burst_earns_the_bucket_nothing(monkeypatch, bench):
    """A minute passes between any two reads of this clock, which refills the bucket every time.

    With the bench reading the real clock, all sixteen callers are granted, and that would read as
    an excess of twelve. It reads none of it, so the grant count stays at the bucket's contents.
    """
    ticks = iter(range(0, 60_000_000, 60))
    monkeypatch.setattr(time, "monotonic", lambda: float(next(ticks)))
    assert bench._one_burst(4, 16) == 4
