import time
from types import SimpleNamespace

import capacity_bench

import store


def test_rooms_cache_bench_can_write_notes_at_capacity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(store, "MAX_ROOMS", 2)
    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", 512)
    capacity_bench.build(tmp_path, scale=1.0)

    # Drive each arm long enough for a note overwrite and a /rooms sample, even on a busy host.
    ticks = iter((0.0, 0.0, 0.1, 1.0, 1.0, 1.1, 2.0, 2.0, 2.1))
    monkeypatch.setattr(
        capacity_bench,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(ticks), perf_counter=time.perf_counter, sleep=lambda _: None
        ),
    )
    capacity_bench.rooms_cache_bench(tmp_path, seconds=0.05)

    output = capsys.readouterr().out
    assert output.count("walks /   1 requests") == 3
    assert store.counters(tmp_path)["notes_written"] == 3
    assert store.note_stats(tmp_path)["total"] == store.MAX_NOTES_TOTAL
