"""Run: uv run --group dev python -m pytest tests

Padding the end of a message with something invisible is the cheapest thing a farm can try against
the cross-sender duplicate filter: every URL write lane ends in a free-form segment, so it costs
three characters per copy and needs no key, no signature and no second room. The message a human
reads is unchanged, which is the whole point of trying it.

It does not work, and not for the reason the documents give. `limit.normalize_text` maps every
category in `store.INVISIBLE_CATEGORIES` to a space and then collapses runs, so trailing padding is
gone before the ring key exists — one slot, however the copy was spelled. The router is not what
stops it: the path convertor is `.*` without DOTALL, which matches every character except a
newline, so a trailing `%0D` or `%09` reaches the operation on every lane.

`tests/unit/test_dupe_ring.py` pins the other axis — one text keys the same whether the lane swept
it first — over every code point either transform touches, with the probe character embedded in the
middle of the phrase. That is the axis a lane chooses. This file is the axis a caller chooses: text
against text-plus-padding, at the end of the segment, which is the only part of a message a farm
can vary without also changing what the room sees.

Every assertion here is on what the padded copy achieves, never on which refusal it collects, so
none of them depends on where the router happens to draw its line.
"""

from __future__ import annotations

import unicodedata
from contextlib import contextmanager

import _client
import pytest

import config
import limit
import store

client = _client.client  # the shared TestClient fixture

# The values under test, pinned in one visible place, matching test_dupe_filter.py because both
# files assert the same mechanism at the same numbers. Deliberate choices, not echoes of config.py
# — a retune there must not silently re-tune what these assertions mean. Restated rather than
# imported: `pythonpath` carries `src` and `tests`, not `tests/http`.
WINDOW = 60
COPIES = 5
FLOOR = 16

# Long enough to clear the floor with margin, and shaped like the measured farm phrases rather than
# like prose a test invented.
PHRASE = "checking node health... all good. $flop network participation confirmed."

# What a farm can put at the end of a segment, by category, written as escapes rather than
# literals: an invisible character in a source file is the exact hazard this service sweeps, and a
# reader has to be able to see why the test works. `Cc` is what the write lanes sweep and what a
# raw `%0D` belongs to; `Cf` is here because a zero-width space is the padding a caller reaches for
# first. Each case carries both spellings — the character the key is built from, and the
# percent-encoding that puts it in a URL — so a mismatch between them cannot pass as a refusal.
#
# Both categories are here because they are stopped by different rungs, which is not visible from
# a green run: delete the sweep from normalize_text and the CR and TAB cases still pass, because
# `str.split()` drops trailing ASCII whitespace on its own. Only the zero-width space needs the
# sweep. Keep both, or half the mechanism is untested while the file still reads as covering it.
PADDING = [
    pytest.param("\r", "%0D", id="carriage-return-Cc"),
    pytest.param("\r\r\r", "%0D%0D%0D", id="carriage-return-run-Cc"),
    pytest.param("\t", "%09", id="tab-Cc"),
    pytest.param("\u200b", "%E2%80%8B", id="zero-width-space-Cf"),
]


@contextmanager
def _filter_on(**kwargs):
    """Every knob the filter reads, set to the values under test.

    RATE_WRITE rides along because these tests post more writes in a minute than one bucket allows.
    """
    knobs = {
        "DUPE_FILTER_SECONDS": WINDOW,
        "DUPE_MAX_COPIES": COPIES,
        "DUPE_MIN_LENGTH": FLOOR,
        "RATE_WRITE": 600,
    }
    knobs.update(kwargs)
    with config.override(**knobs):
        yield


def _view(client, room: str) -> list[str]:
    # limit=200: the default view is the newest 50, and the counts below are exact.
    return [
        m["text"] for m in client.get("/r/" + room + "?format=json&limit=200").json()["messages"]
    ]


def _say(client, room: str, nick: str, segment: str):
    # Spaces %-encoded rather than trusted to the transport, and callers pass padding already
    # encoded: letting httpx decide how to spell a control character would be testing httpx too.
    return client.get("/r/" + room + "/say/" + nick + "/" + segment.replace(" ", "%20"))


def _fill_threshold(client, room: str) -> None:
    """COPIES distinct senders, one phrase — the room is now at its limit for that text."""
    for i in range(COPIES):
        assert _say(client, room, "nick" + str(i), PHRASE).status_code == 200


def test_no_trailing_invisible_character_makes_a_second_ring_slot() -> None:
    """The mechanism, over every code point the store treats as invisible.

    Exhaustive rather than exemplary for the same reason the sweep test in test_dupe_ring.py is:
    whether a trailing invisible survives into the key is a fact about the Unicode categories and
    about `str.split()` dropping trailing whitespace, neither of which lives in this repo, so it
    can stop being true with no commit here at all and a handful of examples would not notice. One
    divergent code point is one unbounded family of keys for one text — the phrase, the phrase plus
    that character, the phrase plus two of them — and a farm needs no more than that.

    Normalised forms are compared rather than keys, which is the stronger statement (equal
    normalised text is an equal key by construction) and which names the offending character on
    failure instead of showing two digests.
    """
    normalised = limit.normalize_text(PHRASE)
    assert len(normalised) > FLOOR, "a phrase the floor exempts would assert nothing"

    divergent = [
        cp
        for cp in range(0x110000)
        if unicodedata.category(chr(cp)) in store.INVISIBLE_CATEGORIES
        and limit.normalize_text(PHRASE + chr(cp)) != normalised
    ]

    assert not divergent, (
        f"{len(divergent)} invisible code points survive into the ring key when appended, e.g. "
        f"U+{divergent[0]:04X} ({unicodedata.category(chr(divergent[0]))}): one phrase plus that "
        f"character repeated is an unbounded supply of distinct keys for one visible message"
    )


@pytest.mark.parametrize(("raw", "encoded"), PADDING)
def test_padding_the_end_of_a_segment_does_not_buy_a_sixth_copy(client, raw, encoded) -> None:
    """The end-to-end consequence, on the lane where the padding is free.

    Five senders fill the threshold, so the next copy of that phrase is refused whoever sends it.
    The sixth pads the end — a different URL, a different byte string, the same message — and
    collects the filter's own 422 rather than the limiter's 429 or the router's 404. Which refusal
    it is matters: a 404 here would mean the router had caught it and this file was asserting
    nothing about the ring.

    Keyed on `duplicate text`, the phrase that names the refusal, rather than on any of the advice
    beside it — #687 rewrote that advice to remove the escape hatches, and the wording is theirs to
    change again. The status code alone is not enough: 422 is also what a malformed body collects.
    """
    with _filter_on():
        _fill_threshold(client, "pad-room")
        sixth = _say(client, "pad-room", "someone-else", PHRASE + encoded)

    assert sixth.status_code == 422
    assert "duplicate text" in sixth.text and "pad-room" in sixth.text
    assert "429" not in sixth.text and "retry-after" not in sixth.headers
    assert len(_view(client, "pad-room")) == COPIES, "the padded copy must not land"
    assert limit.normalize_text(PHRASE + raw) == limit.normalize_text(PHRASE), (
        "the padded copy normalised to something else, so the 422 above was luck"
    )


def test_a_trailing_newline_buys_no_copy_either_however_it_is_routed(client) -> None:
    """`%0A` is the one spelling whose routing is under discussion, so this asserts only the part
    that does not depend on the answer.

    A single trailing newline currently reaches the operation — `$` also matches immediately before
    one final newline, and the regex hands over a `text` with it already gone — so today this is
    the filter's 422. Were the router tightened to refuse it, it would be a 404. Either way the
    copy does not land, and that is what a room relies on, so that is what is asserted. Pinning the
    status code here would make this test an argument for one side of a question it has no business
    settling.

    The key assertion is not conditional on any of that: however the newline is routed, the ring
    must not see two texts. Without it this test would survive the filter's folding being removed
    entirely, because today the router hands over a `text` with the newline already gone.
    """
    with _filter_on():
        _fill_threshold(client, "nl-room")
        sixth = _say(client, "nl-room", "someone-else", PHRASE + "%0A")

    assert sixth.status_code != 200
    assert len(_view(client, "nl-room")) == COPIES, "the newline-padded copy must not land"
    assert limit.normalize_text(PHRASE + "\n") == limit.normalize_text(PHRASE), (
        "a newline that did reach the ring would key to a second slot"
    )


def test_the_documented_newline_workaround_is_not_a_way_round_the_filter(client) -> None:
    """The 404 for a raw newline tells the caller to "Send the message through the POST lane, which
    accepts newlines and flattens them". That advice must not also be the way past the filter.

    Worth its own test because the flatten and the ring are two steps whose order is not visible
    from either side: this lane takes a real newline in a JSON body rather than a `%0A` in a path,
    so no router is involved and nothing but the sweep rung inside `normalize_text` decides it. A
    caller who follows the documents to the letter meets the same refusal as one who did not.
    """
    with _filter_on():
        _fill_threshold(client, "post-room")
        sixth = client.post("/r/post-room", json={"from": "farm", "text": PHRASE + "\n"})

    assert sixth.status_code == 422
    assert "duplicate text" in sixth.text and "post-room" in sixth.text
    assert len(_view(client, "post-room")) == COPIES, "the flattened copy must not land"


def test_padding_does_not_escape_a_threshold_that_was_filled_on_another_lane(client) -> None:
    """One room, two lanes, one phrase: the threshold filled through POST, the padded copy arriving
    through GET.

    This is the shape a per-lane ring would survive — each lane reaching its own limit, a caller
    alternating them landing twice the copies — while every single-lane assertion above stayed
    green. `test_one_text_takes_one_slot_however_many_lanes_it_arrives_on` pins the four lanes for
    one phrase spelled identically; this adds the part a farm actually has to hand, the phrase
    spelled differently on the second lane. A ring that folded padding only on the lane that swept
    first would pass that test and fail this one.
    """
    with _filter_on():
        for i in range(COPIES):
            posted = client.post("/r/mixed-room", json={"from": "p" + str(i), "text": PHRASE})
            assert posted.status_code == 200
        sixth = _say(client, "mixed-room", "someone-else", PHRASE + "%0D")

    assert sixth.status_code == 422
    assert len(_view(client, "mixed-room")) == COPIES, "the cross-lane padded copy must not land"
