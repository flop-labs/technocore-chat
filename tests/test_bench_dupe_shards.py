"""The offline duplicate benchmark must model independent process-local ring caps."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import limit

BENCH = Path(__file__).resolve().parents[1] / "bench" / "dupe_filter.py"
spec = importlib.util.spec_from_file_location("dupe_filter_shard_bench", BENCH)
assert spec is not None and spec.loader is not None
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def _sharded_traffic() -> tuple[list[tuple[float, str, str]], list[int]]:
    """Each worker stays below 4096 keys, but their combined keys exceed it."""
    rows = [(0.0, "target phrase for the first worker", "target")]
    assignment = [0]
    for i in range(2500):
        rows.extend(
            [
                (float(2 * i + 1), f"unique worker zero phrase {i}", "other"),
                (float(2 * i + 2), f"unique worker one phrase {i}", "other"),
            ]
        )
        assignment.extend([0, 1])
    rows.append((5001.0, "target phrase for the first worker", "target"))
    assignment.append(0)
    return rows, assignment


def test_untimed_replay_keeps_each_worker_ring_independent() -> None:
    rows, assignment = _sharded_traffic()
    limit._dupes.clear()
    refused, total = bench.replay(
        [(text, label) for _, text, label in rows],
        assignment,
        min_length=0,
        max_copies=1,
        window=10_000,
    )
    assert total["target"] == 2
    assert refused["target"] == 1


def test_timed_replay_preserves_worker_cap_and_reports_largest_ring() -> None:
    rows, assignment = _sharded_traffic()
    limit._dupes.clear()
    refused, total, largest_ring = bench.replay_timed(
        rows, assignment, min_length=0, max_copies=1, window=10_000
    )
    assert total["target"] == 2
    assert refused["target"] == 1
    assert largest_ring == 2501
    assert largest_ring < limit.MAX_DUPE_KEYS


def test_timed_replay_reports_peak_even_after_expiry() -> None:
    """An earlier full ring must not be hidden by later expired entries."""
    rows = [
        (float(i) / 1000, f"unique peak occupancy phrase {i}", "other")
        for i in range(limit.MAX_DUPE_KEYS + 1)
    ]
    rows.extend(
        [
            (100.0 + i, f"late fresh occupancy phrase {i}", "other")
            for i in range(3)
        ]
    )
    refused, total, largest_ring = bench.replay_timed(
        rows, [0] * len(rows), min_length=0, max_copies=1, window=5
    )
    assert total["other"] == len(rows)
    assert refused["other"] == 0
    assert largest_ring == limit.MAX_DUPE_KEYS


def test_timed_replay_samples_ring_after_exact_cap_insert() -> None:
    """At the exact cap, sampling before dupe_refused would underreport 4095."""
    rows = [
        (float(i) / 1000, f"exact capacity diagnostic phrase {i}", "other")
        for i in range(limit.MAX_DUPE_KEYS)
    ]
    refused, total, largest_ring = bench.replay_timed(
        rows, [0] * len(rows), min_length=0, max_copies=1, window=5
    )
    assert total["other"] == limit.MAX_DUPE_KEYS
    assert refused["other"] == 0
    assert largest_ring == limit.MAX_DUPE_KEYS
