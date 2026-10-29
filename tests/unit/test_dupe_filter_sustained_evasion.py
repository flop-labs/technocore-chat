"""The sustained evasion class must not turn into a catchable repeated phrase."""

import importlib.util
import random
from pathlib import Path


def test_sustained_evasion_remains_textually_distinct_and_unfiltered():
    spec = importlib.util.spec_from_file_location(
        "dupe_filter_bench", Path(__file__).resolve().parents[2] / "bench" / "dupe_filter.py"
    )
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    rows = bench.sustained_corpus(random.Random(2026), bench.SPAN)
    evasion = [text for _, text, label in rows if label == "evasion"]
    assert len(evasion) == 720  # 60 templates x 12 messages per 120-second capture
    assert len(set(evasion)) == len(evasion)

    bench.limit._dupes.clear()
    refused, total = bench.replay_timed(rows, [0] * len(rows), *bench.CHOSEN)
    assert total["evasion"] == len(evasion)
    assert refused["evasion"] == 0
