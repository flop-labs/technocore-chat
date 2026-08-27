"""Run: uv run --group dev python -m pytest tests

The duplicate ring's own rules, tested against limit.dupe_refused directly - the same
discipline test_dedup.py applies to the retry map. What matters under load is not the
verdict on one message but the bounds: a ring that OOMs is not a filter, a sweep that
empties the whole map in one call is a pause, and a refusal that records a timestamp is
a window a farm can hold open forever.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import limit  # noqa: E402

LONG = "the fourth identical copy of this sentence is refused, measured"


def test_normalisation_folds_case_whitespace_and_unicode_compatibility() -> None:
    """One key per meaning: NFKC first (compatibility forms decompose before casefold),
    then the store's invisible categories to spaces, then casefold, then whitespace
    collapse. Trailing punctuation stays a difference on purpose - measured, stripping it
    catches nothing."""
    a = limit.normalize_text("Checking   Node HEALTH... all good")
    b = limit.normalize_text("checking node health... all good")
    full = limit.normalize_text("\uff23\uff48\uff45\uff43\uff4b\uff49\uff4e\uff47 node health")
    assert a == b
    assert full == limit.normalize_text("checking node health")
    # A zero-width joiner and a line separator are invisible to the store and to this.
    assert limit.normalize_text("con\u200dsensus\u2028node") == limit.normalize_text(
        "con sensus node"
    )


def test_the_length_floor_is_on_the_normalised_text() -> None:
    """16 normalised characters is the boundary: at or above it the filter applies,
    below it never does - the entire conversational-repeat class lives below it."""
    limit._dupes.clear()
    assert limit.dupe_refused("r", "x" * 15, now=0.0, window=60) is False
    assert not limit._dupes, "a short text must not even be recorded"
    for i in range(8):
        assert limit.dupe_refused("r", "x" * 16, now=float(i), window=60) is (i >= 5)


def test_a_refusal_never_extends_the_window() -> None:
    """Only accepts are recorded. A farm hammering the refusal must not push the
    expiry out: the phrase opens again exactly 'window' after the last copy that
    landed, which is what makes the filter survivable to run at all."""
    limit._dupes.clear()
    for i in range(5):
        assert limit.dupe_refused("r", LONG, now=100.0 + i, window=60) is False
    for t in range(105, 160):
        assert limit.dupe_refused("r", LONG, now=float(t), window=60) is True
    # 104.0 was the last accept; 60s later the window is shut on it, refusals notwithstanding.
    assert limit.dupe_refused("r", LONG, now=164.1, window=60) is False


def test_the_ring_stays_bounded_under_a_flood() -> None:
    """The bound that matters under load. Every key here is live - nothing has expired,
    so only the hard cap holds the line, and what survives is the newest."""
    limit._dupes.clear()
    cap = 128
    for i in range(20_000):
        limit.dupe_refused("r", "phrase number " + str(i), now=1000.0, window=300.0, cap=cap)
    assert len(limit._dupes) == cap
    assert limit.dupe_refused("r", "phrase number 19999", now=1000.0, window=300.0) is False
    limit._dupes.clear()


def test_one_write_never_pays_for_the_whole_backlog() -> None:
    """The sweep is capped per call: a burst of expiry cannot turn one accepted write
    into a pause that holds the very lock-free path this filter protects."""
    limit._dupes.clear()
    for i in range(1000):
        limit.dupe_refused("r", "an old phrase number " + str(i), now=0.0, window=1.0, cap=10_000)
    before = len(limit._dupes)
    limit.dupe_refused("r", "a fresh phrase indeed", now=500.0, window=1.0, cap=10_000)
    assert len(limit._dupes) == before - 8 + 1
    limit._dupes.clear()


def test_off_is_one_comparison_and_touches_nothing() -> None:
    """window=0 is the opt-out, so it must cost nothing and record nothing - an operator
    setting CHAT_DUPE_FILTER_SECONDS=0 buys back the pre-filter hot path exactly."""
    limit._dupes.clear()
    assert limit.dupe_refused("r", LONG, now=0.0, window=0) is False
    assert not limit._dupes


def test_rooms_are_isolated_and_threads_of_copies_count_not_senders() -> None:
    """The key has no sender in it - that is the whole point of a cross-sender filter -
    and it has a room in it, so two rooms can hold the same conversation independently."""
    limit._dupes.clear()
    for i in range(5):
        for room in ("lobby", "meta"):
            assert limit.dupe_refused(room, LONG, now=float(i), window=60) is False
    assert limit.dupe_refused("lobby", LONG, now=5.0, window=60) is True
    assert limit.dupe_refused("meta", LONG, now=5.0, window=60) is True
    assert limit.dupe_refused("elsewhere", LONG, now=5.0, window=60) is False
    limit._dupes.clear()
