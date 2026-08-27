"""Regression test: refused duplicate keys must stay MRU, not be silently aged out.

See: dupe_refused() bypass — refusal path skipped move_to_end(key), so an
actively-blocked duplicate could be evicted from the LRU ring by unrelated
writes before its window expired, resetting the max_copies counter.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import limit


def test_hot_refused_key_survives_interleaved_lru_pressure():
    limit._dupes.clear()
    now = 1000.0
    window = 60.0
    cap = 4
    room = "room"
    text = "this is a repeated message"

    for i in range(5):
        assert limit.dupe_refused(
            room, text, now + i, window, min_length=1, max_copies=5, cap=cap
        ) is False

    t = now + 5
    bypass_count = 0
    for i in range(10):
        t += 1
        refused = limit.dupe_refused(
            room, text, t, window, min_length=1, max_copies=5, cap=cap
        )
        if not refused:
            bypass_count += 1
        t += 1
        limit.dupe_refused(
            room, f"unique {i}", t, window, min_length=1, max_copies=5, cap=cap
        )

    assert bypass_count == 0, (
        f"duplicate filter bypassed {bypass_count}/10 times within its own window -- "
        "hot key was evicted by unrelated LRU pressure"
    )
