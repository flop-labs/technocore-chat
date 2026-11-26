"""`bench/rooms.py --writers`: the contended arm fails rather than time an idle store (#1129).

A writer thread's exception never reaches the thread that joins it, so before this the arm
returned an ordinary timing row after its only writer had died: the cold arm under the
contended label. These drive the real `contended` against a small real store.

`bench/rooms.py` is a script, not a package: importing it sets `CHAT_FSYNC` and prepends
`src` to `sys.path`. Both are pinned through monkeypatch so neither outlives the test.
"""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

import store

BENCH = Path(__file__).resolve().parents[2] / "bench" / "rooms.py"


@pytest.fixture
def rooms(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_FSYNC", os.environ.get("CHAT_FSYNC", "0"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("bench_rooms_under_test", BENCH)
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    # The script's `@dataclass` resolves annotations through sys.modules[__module__].
    monkeypatch.setitem(sys.modules, "bench_rooms_under_test", bench)
    spec.loader.exec_module(bench)
    assert bench.store is store
    bench.build(tmp_path, rooms=4, deep=2, depth=3)
    return bench, tmp_path


def test_a_writer_that_dies_fails_the_arm(rooms, monkeypatch):
    bench, root = rooms

    def broken(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(store, "_write_record", broken)
    with pytest.raises(RuntimeError, match="a writer failed") as caught:
        bench.contended(root, limit=4, reps=3, writers=1, deep=2)
    assert isinstance(caught.value.__cause__, OSError)


def test_a_writer_that_dies_after_its_first_write_fails_the_arm(rooms, monkeypatch):
    bench, root = rooms
    real = store._write_record
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:
            raise OSError("injected write failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_write_record", flaky)
    with pytest.raises(RuntimeError, match="a writer failed"):
        bench.contended(root, limit=4, reps=3, writers=1, deep=2)


def test_a_writer_stalled_through_the_whole_arm_fails_it(rooms, monkeypatch):
    # Alive but landing nothing while the walk is timed: no exception to propagate, and
    # still not a contended measurement.
    bench, root = rooms
    real = store._write_record
    calls = []

    def stalls(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:
            time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_write_record", stalls)
    with pytest.raises(RuntimeError, match="no write landed"):
        bench.contended(root, limit=4, reps=1, writers=1, deep=2)


def test_healthy_writers_still_produce_a_row(rooms):
    bench, root = rooms
    row = bench.contended(root, limit=4, reps=3, writers=2, deep=2)
    assert set(row) == {"median", "min", "max"}
    assert 0 < row["min"] <= row["median"] <= row["max"]
