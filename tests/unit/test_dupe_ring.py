"""Run: uv run --group dev python -m pytest tests

The duplicate ring's own rules, tested against limit.dupe_refused directly. What
matters under load is not the verdict on one message but the bounds: a ring that OOMs
is not a filter, a sweep that empties the whole map in one call is a pause, and a
refusal that records a timestamp is a window a farm can hold open forever.

Every call passes the whole parameter set explicitly - window, floor, threshold, cap -
rather than leaning on the signature defaults (which mirror the shipped config and
have moved with it). A ring test asserts arithmetic at numbers it chose.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import limit  # noqa: E402

# The values under test, chosen here: 60s window, 16-char floor, fifth copy allowed
# (sixth refused). Deliberate numbers, not echoes of limit.py's defaults.
WINDOW = 60
FLOOR = 16
COPIES = 5
LONG = "one more copy of this sentence than allowed is refused, measured"


def refused(
    text: str,
    now: float,
    room: str = "r",
    *,
    window: float = WINDOW,
    min_length: int = FLOOR,
    max_copies: int = COPIES,
    cap: int = limit.MAX_DUPE_KEYS,
) -> bool:
    """dupe_refused with the values under test, overridable per call the way a test
    varies one knob at a time."""
    return limit.dupe_refused(room, text, now, window, min_length, max_copies, cap)


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
    """The floor is the boundary: at or above it the filter applies, below it never
    does - the entire conversational-repeat class lives below it."""
    limit._dupes.clear()
    assert refused("x" * (FLOOR - 1), now=0.0) is False
    assert not limit._dupes, "a short text must not even be recorded"
    for i in range(COPIES + 3):
        assert refused("x" * FLOOR, now=float(i)) is (i >= COPIES)


def test_a_refusal_never_extends_the_window() -> None:
    """Only accepts are recorded. A farm hammering the refusal must not push the
    expiry out: the phrase opens again exactly 'window' after the last copy that
    landed, which is what makes the filter survivable to run at all."""
    limit._dupes.clear()
    for i in range(COPIES):
        assert refused(LONG, now=100.0 + i) is False
    for t in range(100 + COPIES, 160):
        assert refused(LONG, now=float(t)) is True
    # 100+COPIES-1 was the last accept; WINDOW after it the window is shut on it,
    # refusals notwithstanding.
    assert refused(LONG, now=100.0 + COPIES - 1 + WINDOW + 0.1) is False


def test_the_threshold_decides_which_copy_is_the_refused_one() -> None:
    """COPIES is arithmetic, not a constant of nature: at 2 the third copy is refused.
    Pinned so a retune of the default cannot silently re-tune what this file means."""
    limit._dupes.clear()
    assert refused(LONG, now=0.0, max_copies=2) is False
    assert refused(LONG, now=1.0, max_copies=2) is False
    assert refused(LONG, now=2.0, max_copies=2) is True


def test_the_ring_stays_bounded_under_a_flood() -> None:
    """The bound that matters under load. Every key here is live - nothing has expired,
    so only the hard cap holds the line, and what survives is the newest."""
    limit._dupes.clear()
    cap = 128
    for i in range(20_000):
        refused("phrase number " + str(i), now=1000.0, window=300.0, cap=cap)
    assert len(limit._dupes) == cap
    assert refused("phrase number 19999", now=1000.0, window=300.0, cap=cap) is False
    limit._dupes.clear()


def test_one_write_never_pays_for_the_whole_backlog() -> None:
    """The sweep is capped per call: a burst of expiry cannot turn one accepted write
    into a pause that holds the very lock-free path this filter protects."""
    limit._dupes.clear()
    for i in range(1000):
        refused("an old phrase number " + str(i), now=0.0, window=1.0, cap=10_000)
    before = len(limit._dupes)
    refused("a fresh phrase indeed", now=500.0, window=1.0, cap=10_000)
    assert len(limit._dupes) == before - 8 + 1
    limit._dupes.clear()


def test_off_is_one_comparison_and_touches_nothing() -> None:
    """window=0 is the opt-out, so it must cost nothing and record nothing - an operator
    setting CHAT_DUPE_FILTER_SECONDS=0 buys back the pre-filter hot path exactly."""
    limit._dupes.clear()
    assert refused(LONG, now=0.0, window=0) is False
    assert not limit._dupes


def test_rooms_are_isolated_and_copies_count_not_senders() -> None:
    """The key has no sender in it - that is the whole point of a cross-sender filter -
    and it has a room in it, so two rooms can hold the same conversation independently."""
    limit._dupes.clear()
    for i in range(COPIES):
        for room in ("lobby", "meta"):
            assert refused(LONG, now=float(i), room=room) is False
    assert refused(LONG, now=float(COPIES), room="lobby") is True
    assert refused(LONG, now=float(COPIES), room="meta") is True
    assert refused(LONG, now=float(COPIES), room="elsewhere") is False
    limit._dupes.clear()
