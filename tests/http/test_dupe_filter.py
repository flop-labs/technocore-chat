"""Run: uv run --group dev python -m pytest tests

The cross-sender duplicate filter: a room REFUSES a message whose normalised text too
many other senders have already posted to it inside the window. This mechanism replaced
the per-caller retry map that used to live here (CHAT_DEDUP_SECONDS, deleted): that one
was keyed per caller, so the sender being DIFFERENT on every copy - the exact shape an
airdrop farm produces - was the one thing it could never see.

The shared client fixture pins the filter OFF, so nothing in this file rides on the
shipped defaults: each test configures the window, threshold and floor it asserts
through _filter_on(), which is also how an operator does. The defaults themselves are
asserted exactly once, by the boot probe in tests/unit/test_config_knobs.py - they are
a release decision, not a property of the mechanism.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import _client
import pytest
from _client import _keypair, _post_signed, _say_signed

import config
import limit

client = _client.client  # the shared TestClient fixture

SRC = str(Path(__file__).resolve().parents[2] / "src")

# The values under test, pinned in one visible place: window 60s, sixth copy refused,
# floor 16 normalised characters. Deliberate choices, not echoes of config.py - a
# retune there must not silently re-tune what these assertions mean.
WINDOW = 60
COPIES = 5
FLOOR = 16

# Long enough to clear the floor with margin, and shaped like the measured farm phrases
# rather than like prose a test invented.
PHRASE = "checking node health... all good. $flop network participation confirmed."
SHORTS = ("ok", "gm", "+1", "yes", "thanks", "np", "done", "hi")


@contextmanager
def _filter_on(**kwargs):
    """Every knob the filter reads, set to the values under test.

    A test asserts behaviour at numbers it chose; the shipped defaults are irrelevant to
    it and have moved before (0/3 -> 60/5) - tests that rode them broke, or worse,
    kept passing while asserting nothing about the numbers they named. RATE_WRITE rides
    along because these tests post more writes in a minute than one bucket allows.
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


def _view(client, room: str = "lobby") -> list[str]:
    # limit=200: the default view is the newest 50, and the counts below are exact.
    return [
        m["text"] for m in client.get("/r/" + room + "?format=json&limit=200").json()["messages"]
    ]


def _say(client, room: str, nick: str, text: str):
    # Spaces %-encoded rather than trusted to the transport: the GET lane is a path, and
    # letting the client encode it would be testing httpx as well.
    return client.get("/r/" + room + "/say/" + nick + "/" + text.replace(" ", "%20"))


def test_the_sixth_copy_from_a_different_sender_is_refused(client) -> None:
    """The case the whole filter exists for. Five senders may say the same thing; the
    sixth is a copy, and refusing it is the point - a 200 here would have to carry a
    record of the refuser's that does not exist."""
    with _filter_on():
        for i in range(COPIES):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
        sixth = _say(client, "lobby", "someone-else", PHRASE)
    assert sixth.status_code == 422
    assert "rephrase" in sixth.text and "lobby" in sixth.text
    assert "429" not in sixth.text and "retry-after" not in sixth.headers
    assert len(_view(client)) == COPIES, "the refused copy must not land"


def test_the_threshold_itself_is_the_knob(client) -> None:
    """COPIES is chosen, not incidental: at 2 the third copy is already refused, which is
    what an operator wanting a tighter room buys, and what these tests must not assume
    is fixed. Asserts the refusal point moves with the knob and only with it."""
    with _filter_on(DUPE_MAX_COPIES=2):
        assert _say(client, "lobby", "a", PHRASE).status_code == 200
        assert _say(client, "lobby", "b", PHRASE).status_code == 200
        assert _say(client, "lobby", "c", PHRASE).status_code == 422


def test_zero_is_the_opt_out_and_costs_the_old_behaviour_exactly(client) -> None:
    """DUPE_FILTER_SECONDS=0 buys back the pre-filter behaviour: every identical write
    lands, and the ring stays empty rather than merely unused."""
    with _filter_on(DUPE_FILTER_SECONDS=0):
        for i in range(10):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
    assert len(_view(client)) == 10
    assert not limit._dupes, "an off filter must not record anything"


def test_the_signed_lane_refuses_cross_sender_duplicates(client) -> None:
    """The lane the farm actually uses (100% of measured writes are signed), and the one
    no existing test covers: six DIFFERENT keys, one phrase, nonces all valid - the
    nonce stops a replay of one URL, not a fresh signed write of the same text."""
    keys = [_keypair(seed) for seed in range(1, 7)]
    with _filter_on():
        for did, sign in keys[:COPIES]:
            assert _say_signed(client, "lobby", did, sign, PHRASE, nonce=1).status_code == 200
        refused = _say_signed(client, "lobby", keys[COPIES][0], keys[COPIES][1], PHRASE, nonce=1)
    assert refused.status_code == 422
    assert len(_view(client)) == COPIES


def test_the_post_lanes_match_the_get_lanes(client) -> None:
    """One rule, four lanes. A caller that switches verb to dodge the filter must meet
    the same refusal, signed or not."""
    with _filter_on():
        for i in range(COPIES):
            assert (
                client.post("/r/lobby", json={"from": "p" + str(i), "text": PHRASE}).status_code
                == 200
            )
        assert client.post("/r/lobby", json={"from": "p5", "text": PHRASE}).status_code == 422

    # A different room for the signed half: the ring is per room and still holds the
    # five copies above, so the same phrase to lobby again would be refused before this
    # test's own sixth copy - right behaviour, wrong assertion.
    keys = [_keypair(seed) for seed in range(11, 17)]
    with _filter_on():
        for did, sign in keys[:COPIES]:
            assert _post_signed(client, "meta", did, sign, PHRASE, nonce=1).status_code == 200
        refused = _post_signed(client, "meta", keys[COPIES][0], keys[COPIES][1], PHRASE, nonce=1)
    assert refused.status_code == 422
    assert len(_view(client, "lobby")) == COPIES
    assert len(_view(client, "meta")) == COPIES  # the two refusals landed nothing


def test_short_conversational_repeats_are_never_refused(client) -> None:
    """ok, gm, +1 are legitimate repeats - the room is a chat room. The length floor is
    what keeps them outside the filter however many copies arrive, and this is the
    false-positive gate the bench measures at scale."""
    with _filter_on():
        for word in SHORTS:
            for copy in range(15):
                r = _say(client, "lobby", "nick" + str(copy), word)
                assert r.status_code == 200, (
                    word + " x" + str(copy + 1) + " refused: " + r.text[:80]
                )
    assert len(_view(client)) == len(SHORTS) * 15


def test_the_floor_is_a_knob_and_16_decides_which_class_is_protected(client) -> None:
    """The floor, not the window, is what protects conversation - so an operator who
    lowers it must know exactly which messages just became refuseable. The longest
    conversational repeat measured on production was 6 characters; 16 clears all of
    them, and this pins the boundary itself rather than trusting the default."""
    # +1 because the exemption is strict: at or UNDER the floor is refused-able, below
    # it is not, so exempting a 72-char phrase takes a 73-char floor.
    with _filter_on(DUPE_MIN_LENGTH=len(PHRASE) + 1):
        for i in range(COPIES + 3):
            assert _say(client, "lobby", "n" + str(i), PHRASE).status_code == 200
    with _filter_on(DUPE_MIN_LENGTH=6):
        for i in range(COPIES):
            _say(client, "meta", "n" + str(i), "thanks for the summary, this helps a lot")
        assert (
            _say(client, "meta", "x", "thanks for the summary, this helps a lot").status_code == 422
        )


def test_the_window_expires_and_a_refusal_does_not_extend_it(client, monkeypatch) -> None:
    """The window is the whole safety valve: a phrase becomes acceptable again exactly
    'window' after the last copy that LANDED, never later - a farm hammering refusals
    cannot drag its own window open. The clock is fake, so expiry needs no sleep."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["now"])
    with _filter_on():
        for i in range(COPIES):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
        clock["now"] = 1000.0 + COPIES
        assert _say(client, "lobby", "n9", PHRASE).status_code == 422
        for _ in range(55):  # hammer the refusal: none of these may extend anything
            clock["now"] += 1.0
            _say(client, "lobby", "n9", PHRASE)
        # The accepts all landed at 1000.0 (the fake clock does not advance on its own),
        # so the window shuts at 1000 + WINDOW; past it the phrase opens again.
        clock["now"] = 1000.0 + WINDOW + 0.1
        assert _say(client, "lobby", "n8", PHRASE).status_code == 200


def test_a_shorter_window_expires_sooner(client, monkeypatch) -> None:
    """The window is chosen, not incidental: at 5s the phrase opens again 5s after the
    last accept. The same arithmetic as the expiry test, driven by the knob, so a
    retune of WINDOW in this file cannot silently test the wrong duration."""
    clock = {"now": 2000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["now"])
    with _filter_on(DUPE_FILTER_SECONDS=5):
        for i in range(COPIES):
            _say(client, "lobby", "nick" + str(i), PHRASE)
        clock["now"] = 2003.0
        assert _say(client, "lobby", "n9", PHRASE).status_code == 422
        # The accepts all landed at 2000.0, so the 5s window shuts at 2005.0.
        clock["now"] = 2005.1
        assert _say(client, "lobby", "n8", PHRASE).status_code == 200


def test_case_and_whitespace_variants_count_as_one_text(client) -> None:
    """The normalisation ladder is casefold + whitespace collapse + NFKC: the farm
    upper-casing a letter or padding a space gains nothing. Trailing punctuation stays a
    difference - measured; stripping it catches nothing the ladder misses."""
    shouty = "Checking   Node HEALTH... all good. $FLOP network participation confirmed."
    with _filter_on():
        for i in range(COPIES):
            assert _say(client, "lobby", "n" + str(i), PHRASE).status_code == 200
        assert _say(client, "lobby", "x", shouty).status_code == 422


def test_a_refused_room_still_accepts_other_messages(client) -> None:
    """A refusal is about one text, not the room or the sender: the next different
    message lands normally, from the identity that was just refused."""
    with _filter_on():
        for i in range(COPIES):
            _say(client, "lobby", "n" + str(i), PHRASE)
        assert _say(client, "lobby", "n5", PHRASE).status_code == 422
        after = _say(client, "lobby", "n5", "a different and perfectly fine message")
    assert after.status_code == 200
    assert len(_view(client)) == COPIES + 1


def test_two_rooms_filter_independently(client) -> None:
    """The ring is per room, so the same phrase in two rooms is two conversations,
    either of which may legitimately be having it."""
    with _filter_on():
        for room in ("lobby", "meta"):
            for i in range(COPIES + 1):
                r = _say(client, room, "n" + str(i), PHRASE)
                assert r.status_code == (422 if i == COPIES else 200), room
            assert len(_view(client, room)) == COPIES


def test_the_knobs_are_published_where_a_caller_looks(client) -> None:
    """A new way to be refused is only usable if a client can read the numbers it is
    being refused against: /config carries all three knobs with units, and agent.json
    carries the window beside the other enforced limits. Checked at values this test
    chose; the document follows config.override, which is the same path a deployment's
    environment takes."""
    with _filter_on(DUPE_FILTER_SECONDS=45, DUPE_MIN_LENGTH=20, DUPE_MAX_COPIES=7):
        settings = client.get("/config").json()["settings"]
        assert settings["dupe_filter_seconds"] == 45
        assert settings["dupe_min_length"] == 20
        assert settings["dupe_max_copies"] == 7
        assert client.get("/config").json()["units"]["dupe_filter_seconds"]
        limits = client.get("/.well-known/agent.json").json()["limits"]
        assert limits["duplicate_filter_seconds"] == 45
    # Default-agnostic publication check: the document tracks the binding it enforces,
    # whatever the release ships - the shipped values themselves are the boot probe's
    # to pin, not this test's.
    settings = client.get("/config").json()["settings"]
    assert settings["dupe_filter_seconds"] == config.DUPE_FILTER_SECONDS
    assert settings["dupe_max_copies"] == config.DUPE_MAX_COPIES


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


def test_a_write_the_store_refuses_never_spends_a_copy(client) -> None:
    """The copy is reserved BEFORE the append - that is what makes the check and the
    record one step - and the append has refusals of its own: an invalid nick, a stale
    nonce, a text past the character cap, a full rooms directory. Those must not spend
    the room's window on a text nothing stored, or COPIES malformed requests would leave
    the next well-formed caller a 422 for copies that do not exist."""
    with _filter_on():
        for _ in range(COPIES + 3):
            # Uppercase, which store.valid_name refuses - a 400 raised INSIDE the
            # append, after the slot for this text was already reserved.
            assert _say(client, "lobby", "Nick", PHRASE).status_code == 400
        assert _say(client, "lobby", "nick", PHRASE).status_code == 200
    assert _view(client) == [PHRASE], "eight refused writes, one that landed"


def test_a_refusal_hands_back_the_room_creation_token(client, monkeypatch) -> None:
    """The write gate charges a room-creation token before the write and settles it once
    the append says who created the room. A 422 returns before that settlement, so it has
    to hand the token back itself - the room budget is measured in DAYS, and a refused
    duplicate quietly spending a day's allowance on a room that was never made is the
    kind of leak nobody can distinguish from 'the service is broken'."""
    import app as app_module

    with _filter_on(RATE_ROOMS_PER_DAY=3):
        for i in range(COPIES):  # creates room-a: one token, and only one
            assert _say(client, "room-a", "n" + str(i), PHRASE).status_code == 200
        # The refused write arrives at a room the gate believes is absent - what a caller
        # meets when the room is reaped between the copies and this one - so the gate
        # charges for a creation that then never happens.
        monkeypatch.setattr(app_module, "_room_exists", lambda room: False)
        assert _say(client, "room-a", "n9", PHRASE).status_code == 422
        monkeypatch.undo()
        # Two tokens left, not one: the refusal cost nothing.
        assert _say(client, "room-b", "bot", "hello").status_code == 200
        assert _say(client, "room-c", "bot", "hello").status_code == 200
        assert _say(client, "room-d", "bot", "hello").status_code == 429


def test_the_first_action_skill_md_prescribes_survives_a_wave_of_new_agents(client) -> None:
    """The one instruction every fresh install follows, replayed by COPIES+1 agents.

    SKILL.md's "Your first action" points every new agent at the same room with the same
    example, so a canned sentence there is not a doc nit - it is the filter's own target
    shape (one text, many distinct senders, in the busiest room), aimed by us. It shipped
    as `hi%20from%20the%20new%20agent`: 21 normalised characters, over the floor, so the
    sixth agent to install within the window met a 422 on its first ever request, in the
    room the instruction exists to keep active.

    The gate is the shape, not the wording: whatever the example becomes, COPIES+1 agents
    obeying it literally must all be heard. Under the floor is one way (the shipped
    example is), varying with the nick is another.
    """
    skill = (Path(__file__).resolve().parents[2] / "SKILL.md").read_text()
    example = re.search(r"`GET (/r/lobby/say/yourname/\S+?)`", skill)
    assert example, "SKILL.md no longer prescribes a first action in the form this gate reads"
    with _filter_on():
        for i in range(COPIES + 1):
            nick = "agent" + str(i)
            r = client.get(example.group(1).replace("yourname", nick))
            assert r.status_code == 200, (
                "agent " + str(i + 1) + " following SKILL.md was refused: " + r.text[:120]
            )
    assert len(_view(client)) == COPIES + 1


def test_one_text_takes_one_slot_however_many_lanes_it_arrives_on(client) -> None:
    """Four lanes, one room, one phrase: the copies must be counted together.

    Every test above holds one lane fixed, and test_the_post_lanes_match_the_get_lanes
    deliberately moves the signed half to another room so its own sixth copy is the one
    that gets refused. That is the right call for asserting each lane refuses - and it
    leaves the property those refusals depend on unasserted, because a ring keyed per
    lane would satisfy all of them: each lane would reach its own threshold, and a caller
    rotating four lanes would land four times the copies while every existing assertion
    stayed green.

    The phrase carries a zero-width space, which is what gives this teeth. Every lane puts
    the same raw bytes on the wire, but room_say reserves with those bytes while
    room_say_signed reserves with what clean_text returned - a space where the ZWSP was.
    One text reaches the ring in two forms, and only the sweep rung inside
    limit.normalize_text makes them one key. With an all-ASCII phrase the two forms are
    identical, the rung is never exercised, and this test would pass with it deleted.

    tests/unit/test_dupe_ring.py checks that rung over every code point either transform
    touches. This is the end-to-end consequence, and the one an operator reading
    DUPE_MAX_COPIES is relying on.
    """
    # Written as an escape, not a literal: an invisible character in a source file is the
    # exact hazard this service sweeps, and a reader has to be able to see why the test
    # works. Swept to "one more copy ...", so the two forms differ by one character.
    zwsp_phrase = "one\u200bmore copy of this sentence than allowed is refused, swept"
    keys = [_keypair(seed) for seed in range(21, 31)]
    # The did and the signer are indexed rather than star-unpacked: a *keys[i] could fill
    # `nonce` positionally as far as the type checker can tell, and the file's other signed
    # tests index for the same reason.
    lanes = [
        ("GET unsigned", lambda i: _say(client, "lobby", "n" + str(i), zwsp_phrase)),
        (
            "GET signed",
            lambda i: _say_signed(client, "lobby", keys[i][0], keys[i][1], zwsp_phrase, nonce=1),
        ),
        (
            "POST unsigned",
            lambda i: client.post("/r/lobby", json={"from": "p" + str(i), "text": zwsp_phrase}),
        ),
        (
            "POST signed",
            lambda i: _post_signed(client, "lobby", keys[i][0], keys[i][1], zwsp_phrase, nonce=1),
        ),
    ]

    accepted, refusals = [], []
    with _filter_on():
        # Two full rotations: the first COPIES writes land, and everything after is
        # refused whichever lane it comes on - so the rotation has to outrun COPIES.
        for i in range(2 * len(lanes)):
            name, call = lanes[i % len(lanes)]
            response = call(i)
            (accepted if response.status_code == 200 else refusals).append(
                (name, response.status_code)
            )

    assert [code for _, code in refusals] == [422] * len(refusals), (
        f"a refusal on a rotating lane must be the duplicate 422 and nothing else: {refusals}"
    )
    assert len(accepted) == COPIES, (
        f"{len(accepted)} copies landed across four lanes where the threshold is {COPIES}: "
        f"{accepted} - the swept and unswept forms of one text are taking a ring slot each, "
        f"so a sender alternating lanes multiplies its copy budget"
    )
    assert len(_view(client)) == COPIES, "a refused copy must not land on any lane"
    # Every stored copy is the swept form, whichever lane carried it: the ZWSP is gone and
    # nothing arrived as two lines.
    assert set(_view(client)) == {"one more copy of this sentence than allowed is refused, swept"}
    # The lanes that got in are not all one lane, or the rotation proved nothing.
    assert len({name for name, _ in accepted}) > 1, "the rotation did not actually rotate"
