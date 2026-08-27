"""Run: uv run --group dev python -m pytest tests

The cross-sender duplicate filter: a room REFUSES a message whose normalised text too
many other senders have already posted to it inside the window. This mechanism replaced
the per-caller retry map that used to live here (CHAT_DEDUP_SECONDS, deleted): that one
was keyed per caller, so the sender being DIFFERENT on every copy - the exact shape an
airdrop farm produces - was the one thing it could never see.

Off by default (see config.DUPE_FILTER_SECONDS); every test here enables it explicitly,
which is also how an operator does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import _client
import pytest
from _client import _keypair, _post_signed, _say_signed

import config
import limit

client = _client.client  # the shared TestClient fixture

SRC = str(Path(__file__).resolve().parents[2] / "src")

# Long enough to clear DUPE_MIN_LENGTH (16) with margin, and shaped like the measured
# farm phrases rather than like prose a test invented.
PHRASE = "checking node health... all good. $flop network participation confirmed."
SHORTS = ("ok", "gm", "+1", "yes", "thanks", "np", "done", "hi")


def _view(client, room: str = "lobby") -> list[str]:
    # limit=200: the default view is the newest 50, and the counts below are exact.
    return [
        m["text"] for m in client.get("/r/" + room + "?format=json&limit=200").json()["messages"]
    ]


def _say(client, room: str, nick: str, text: str):
    # Spaces %-encoded rather than trusted to the transport: the GET lane is a path, and
    # letting the client encode it would be testing httpx as well.
    return client.get("/r/" + room + "/say/" + nick + "/" + text.replace(" ", "%20"))


def test_on_by_default_the_sixth_copy_from_a_different_sender_is_refused(client) -> None:
    """The default is the decision this release made: the filter is ON at 60s/5 copies,
    so a deployment that sets nothing gets the behaviour - five senders may say the same
    thing, the sixth is a copy. A 200 here would have to carry a record of the refuser's
    that does not exist."""
    assert config.DUPE_FILTER_SECONDS == 60 and config.DUPE_MAX_COPIES == 5
    with config.override(RATE_WRITE=600):
        for i in range(5):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
        sixth = _say(client, "lobby", "someone-else", PHRASE)
    assert sixth.status_code == 422
    assert "rephrase" in sixth.text and "lobby" in sixth.text
    assert "429" not in sixth.text and "retry-after" not in sixth.headers
    assert len(_view(client)) == 5, "the refused copy must not land"


def test_zero_is_the_opt_out_and_costs_the_old_behaviour_exactly(client) -> None:
    """CHAT_DUPE_FILTER_SECONDS=0 buys back the pre-filter behaviour: every identical
    write lands, and the ring stays empty rather than merely unused."""
    with config.override(DUPE_FILTER_SECONDS=0, RATE_WRITE=600):
        for i in range(10):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
    assert len(_view(client)) == 10
    assert not limit._dupes, "an off filter must not record anything"


def test_the_signed_lane_refuses_cross_sender_duplicates(client) -> None:
    """The lane the farm actually uses (100% of measured writes are signed), and the one
    no existing test covers: six DIFFERENT keys, one phrase, nonces all valid - the
    nonce stops a replay of one URL, not a fresh signed write of the same text."""
    keys = [_keypair(seed) for seed in range(1, 7)]
    with config.override(RATE_WRITE=600):
        for did, sign in keys[:5]:
            assert _say_signed(client, "lobby", did, sign, PHRASE, nonce=1).status_code == 200
        refused = _say_signed(client, "lobby", keys[5][0], keys[5][1], PHRASE, nonce=1)
    assert refused.status_code == 422
    assert len(_view(client)) == 5


def test_the_post_lanes_match_the_get_lanes(client) -> None:
    """One rule, four lanes. A caller that switches verb to dodge the filter must meet
    the same refusal, signed or not."""
    with config.override(RATE_WRITE=600):
        for i in range(5):
            assert (
                client.post("/r/lobby", json={"from": "p" + str(i), "text": PHRASE}).status_code
                == 200
            )
        assert client.post("/r/lobby", json={"from": "p5", "text": PHRASE}).status_code == 422

    # A different room for the signed half: the ring is per room and still holds the
    # five copies above, so the same phrase to lobby again would be refused before this
    # test's own sixth copy - right behaviour, wrong assertion.
    keys = [_keypair(seed) for seed in range(11, 17)]
    with config.override(RATE_WRITE=600):
        for did, sign in keys[:5]:
            assert _post_signed(client, "meta", did, sign, PHRASE, nonce=1).status_code == 200
        refused = _post_signed(client, "meta", keys[5][0], keys[5][1], PHRASE, nonce=1)
    assert refused.status_code == 422
    assert len(_view(client, "lobby")) == 5
    assert len(_view(client, "meta")) == 5  # the two refusals landed nothing


def test_short_conversational_repeats_are_never_refused(client) -> None:
    """ok, gm, +1 are legitimate repeats - the room is a chat room. The length floor is
    what keeps them outside the filter however many copies arrive, and this is the
    false-positive gate the bench measures at scale."""
    with config.override(RATE_WRITE=600):
        for word in SHORTS:
            for copy in range(15):
                r = _say(client, "lobby", "nick" + str(copy), word)
                assert r.status_code == 200, (
                    word + " x" + str(copy + 1) + " refused: " + r.text[:80]
                )
    assert len(_view(client)) == len(SHORTS) * 15


def test_the_window_expires_and_a_refusal_does_not_extend_it(client, monkeypatch) -> None:
    """The window is the whole safety valve: a phrase becomes acceptable again exactly
    'window' after the last copy that LANDED, never later - a farm hammering refusals
    cannot drag its own window open. The clock is fake, so expiry needs no sleep."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["now"])
    with config.override(RATE_WRITE=600):
        for i in range(5):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
        clock["now"] = 1005.0
        assert _say(client, "lobby", "n9", PHRASE).status_code == 422
        for _ in range(55):  # hammer the refusal: none of these may extend anything
            clock["now"] += 1.0
            _say(client, "lobby", "n9", PHRASE)
        clock["now"] = 1064.1  # 60.1s after the last ACCEPT (1004.0), past the window
        assert _say(client, "lobby", "n8", PHRASE).status_code == 200


def test_case_and_whitespace_variants_count_as_one_text(client) -> None:
    """The normalisation ladder is casefold + whitespace collapse + NFKC: the farm
    upper-casing a letter or padding a space gains nothing. Trailing punctuation stays a
    difference - measured; stripping it catches nothing the ladder misses."""
    shouty = "Checking   Node HEALTH... all good. $FLOP network participation confirmed."
    with config.override(RATE_WRITE=600):
        for i in range(5):
            assert _say(client, "lobby", "n" + str(i), PHRASE).status_code == 200
        assert _say(client, "lobby", "x", shouty).status_code == 422


def test_a_refused_room_still_accepts_other_messages(client) -> None:
    """A refusal is about one text, not the room or the sender: the next different
    message lands normally, from the identity that was just refused."""
    with config.override(RATE_WRITE=600):
        for i in range(5):
            _say(client, "lobby", "n" + str(i), PHRASE)
        assert _say(client, "lobby", "n5", PHRASE).status_code == 422
        after = _say(client, "lobby", "n5", "a different and perfectly fine message")
    assert after.status_code == 200
    assert len(_view(client)) == 6


def test_two_rooms_filter_independently(client) -> None:
    """The ring is per room, so the same phrase in two rooms is two conversations,
    either of which may legitimately be having it."""
    with config.override(RATE_WRITE=600):
        for room in ("lobby", "meta"):
            for i in range(6):
                r = _say(client, room, "n" + str(i), PHRASE)
                assert r.status_code == (422 if i == 5 else 200), room
            assert len(_view(client, room)) == 5


def test_the_window_is_published_where_a_caller_looks(client) -> None:
    """A new way to be refused is only usable if a client can read the numbers it is
    being refused against: /config carries all three knobs with units, and agent.json
    carries the window beside the other enforced limits."""
    with config.override(DUPE_FILTER_SECONDS=45, DUPE_MIN_LENGTH=20, DUPE_MAX_COPIES=7):
        doc = client.get("/config").json()
        assert doc["settings"]["dupe_filter_seconds"] == 45
        assert doc["settings"]["dupe_min_length"] == 20
        assert doc["settings"]["dupe_max_copies"] == 7
        assert doc["units"]["dupe_filter_seconds"]
        limits = client.get("/.well-known/agent.json").json()["limits"]
        assert limits["duplicate_filter_seconds"] == 45
    # ...and the defaults publish as the enforcement they are, not as absent keys: a
    # caller must be able to tell "60s window, 5 copies" from "unknown filter".
    settings = client.get("/config").json()["settings"]
    assert settings["dupe_filter_seconds"] == 60
    assert settings["dupe_max_copies"] == 5


def test_a_refusal_is_documented_on_every_write_lane(client) -> None:
    """The spec lists the 422 on all three write operations, or a contract-fuzzing
    client reads it as a transport fault and retries the identical bytes."""
    doc = client.get("/openapi.json").json()["paths"]
    for path in ("/r/{room}/say/{nick}/{text}", "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}"):
        assert "422" in doc[path]["get"]["responses"], path
    assert "422" in doc["/r/{room}"]["post"]["responses"]
    assert (
        "duplicate" in doc["/r/{room}/say/{nick}/{text}"]["get"]["responses"]["422"]["description"]
    )


@pytest.mark.parametrize("raw", ["soon", "inf", "nan"])
def test_a_non_finite_window_refuses_to_boot(raw: str) -> None:
    """The window is published at /config, so its finiteness is a contract - the same
    rule CHAT_MAX_WAIT and the cache windows already follow (_finite_env)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    boot = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, " + repr(SRC) + "); import app"],
        capture_output=True,
        text=True,
        env={**clean, "CHAT_DUPE_FILTER_SECONDS": raw},
    )
    assert boot.returncode != 0, "app booted with CHAT_DUPE_FILTER_SECONDS=" + repr(raw)
    # 'inf'/'nan' reach the finite check; 'soon' dies in float() itself - the same loud
    # import-time death every int() knob already has.
    assert "must be a finite number" in boot.stderr or "could not convert" in boot.stderr
