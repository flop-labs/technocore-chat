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
import time
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


def _say(client, room: str, nick: str, text: str, ref: str = ""):
    # Spaces %-encoded rather than trusted to the transport: the GET lane is a path, and
    # letting the client encode it would be testing httpx as well.
    url = "/r/" + room + "/say/" + nick + "/" + text.replace(" ", "%20")
    return client.get(url + ("?ref=" + ref if ref else ""))


def test_the_sixth_copy_from_a_different_sender_is_refused(client) -> None:
    """The case the whole filter exists for. Five senders may say the same thing; the
    sixth is a copy, and refusing it is the point - a 200 here would have to carry a
    record of the refuser's that does not exist."""
    with _filter_on():
        for i in range(COPIES):
            assert _say(client, "lobby", "nick" + str(i), PHRASE).status_code == 200
        sixth = _say(client, "lobby", "someone-else", PHRASE)
    assert sixth.status_code == 422
    assert "/patterns.md" in sixth.text and "lobby" in sixth.text
    assert "rephrase" not in sixth.text and "short" not in sixth.text  # no escape hatch
    assert "429" not in sixth.text and "retry-after" not in sixth.headers
    assert len(_view(client)) == COPIES, "the refused copy must not land"


def test_a_refusal_is_counted_and_logged_so_the_advice_can_be_measured(client, capsys) -> None:
    """Whether the 422 body changes behaviour shows up only in what the refused caller does
    next. The refusal is counted beside rate_limited for /stats, and at CHAT_DEBUG=1 it
    logs the client IP in the field `take` already logs, so a refusal joins to that IP's
    following reads and writes. Off the ladder it logs nothing: it sits on the write path."""
    before = limit._requests["duplicate"]
    with _filter_on(DUPE_MAX_COPIES=1), config.override(DEBUG=1):
        assert _say(client, "lobby", "a", PHRASE).status_code == 200
        assert _say(client, "lobby", "b", PHRASE).status_code == 422
    assert limit._requests["duplicate"] == before + 1
    line = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("duplicate ")]
    assert len(line) == 1 and "ip=" in line[0] and "room=lobby" in line[0]
    with _filter_on(DUPE_MAX_COPIES=1):
        assert _say(client, "lobby", "c", PHRASE).status_code == 422
    assert not [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("duplicate ")]


def test_the_ref_token_is_handed_out_seen_again_and_never_a_way_past_the_filter(
    client, capsys
) -> None:
    """The 422 carries `422-<hex>-<hex>` and asks for it back as ?ref=. Sent back, it is
    counted (requests.followed) and logged once per request — on a read, on the docs the
    body points at, and once (not twice) on a write that also creates a room — and the
    handler otherwise ignores it. Only the exact token shape is counted or logged: a
    forged value with a newline in it must not reach the operator's log at all. Pasted
    into the text instead, glued to a word or not, the token is cut out before the copy
    check — it must not be the thing that makes the sixth copy land."""
    with _filter_on(DUPE_MAX_COPIES=1):
        assert _say(client, "lobby", "a", PHRASE).status_code == 200
        refused = _say(client, "lobby", "b", PHRASE)
    assert refused.status_code == 422
    handed = re.search(r"&ref=(422-[0-9a-f]+-[0-9a-f]{4})", refused.text)
    assert handed, refused.text
    ref = handed.group(1)
    assert "422-" + format(int(time.time()), "x")[:5] in ref, "the token carries its issue second"
    before = limit._requests["followed"]
    with config.override(DEBUG=1):
        assert client.get("/r/lobby?format=json&ref=" + ref).status_code == 200
        assert client.get("/patterns.md?ref=" + ref).status_code == 200
        assert (
            _say(client, "p-fresh-room-for-ref", "b", "a real answer to a", ref).status_code == 200
        )
        assert client.get("/r/lobby?ref=x%0Aduplicate%20ip=forged").status_code == 200
    assert limit._requests["followed"] == before + 3
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("followed ")]
    assert [ln for ln in lines if "ref=" + ref in ln] == lines and len(lines) == 3
    assert "path='/patterns.md'" in lines[1] and "forged" not in "".join(lines)
    with _filter_on(DUPE_MAX_COPIES=1):
        assert _say(client, "lobby", "c", PHRASE + " " + ref).status_code == 422
        assert _say(client, "lobby", "c", PHRASE + ref).status_code == 422  # glued to a word
        assert _say(client, "lobby", "c", "&ref=" + ref + " " + PHRASE).status_code == 422


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
    assert not limit._rings, "and the window's 0 turns the share cap's ring off with it"


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
    """A write the store refuses must not spend the room's window on a text nothing stored,
    or COPIES malformed requests would leave the next well-formed caller a 422 for copies
    that do not exist. The reservation now happens INSIDE the append, keyed by the generation
    the write settles on (#734); the store's own refusals - an invalid nick, a stale nonce, a
    full rooms directory - are checked BEFORE it, so they never reserve, and a failure AFTER
    it hands the slot back (test_a_store_failure_after_the_reservation_hands_the_slot_back).

    Drives the pre-reservation half: an uppercase nick store.valid_name rejects at the top of
    the write, well before the room lock the reservation is taken under."""
    with _filter_on():
        for _ in range(COPIES + 3):
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


def test_a_fixed_interval_repeater_is_capped_by_its_share_of_the_room(client, monkeypatch) -> None:
    """The window's blind spot, at the numbers it was measured at (issue #697).

    One signed key posted one 83-character sentence to `mb-jinken` every 137s against a
    120s window: only 21 of the 67 gaps were inside the window, so the copy count almost
    never reached the threshold, and 67 of the room's 117 records were that one sentence.
    `mb-` rooms cannot be owned, so there was no allow-list, mute or delete to fall back
    on - the window was the only remedy, and a repeater that sleeps past it is the one
    shape it cannot see.

    The share cap is what refuses it: one text may hold DUPE_SHARE of the room's last
    DUPE_RING filterable messages, and the copy that would take more than that is refused
    however long ago the last one landed. The second half is the old behaviour, reached by
    lifting only the share so the window is the only rule left - every copy lands, which
    is exactly what the export showed.
    """
    clock = {"now": 5000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["now"])
    did, sign = _keypair(41)
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    with _filter_on(DUPE_FILTER_SECONDS=120):
        for i in range(allowed):
            r = _say_signed(client, "mb-jinken", did, sign, PHRASE, nonce=i + 1)
            assert r.status_code == 200, "copy " + str(i + 1) + " refused: " + r.text[:120]
            clock["now"] += 137.0  # the measured median gap: always outside the window
        refused = _say_signed(client, "mb-jinken", did, sign, PHRASE, nonce=allowed + 1)
    assert refused.status_code == 422, "a repeater below the window is still a repeater"
    assert str(limit.DUPE_RING) in refused.text and f"{limit.DUPE_SHARE:.0%}" in refused.text
    assert len(_view(client, "mb-jinken")) == allowed

    # Same key, same interval, same room class, share cap lifted out of reach: this is the
    # filter as it shipped, and it refuses nothing.
    monkeypatch.setattr(limit, "DUPE_SHARE", 10.0)
    with _filter_on(DUPE_FILTER_SECONDS=120):
        for i in range(allowed + 1):
            assert (
                _say_signed(client, "mb-other", did, sign, PHRASE, nonce=i + 1).status_code == 200
            )
            clock["now"] += 137.0
    assert len(_view(client, "mb-other")) == allowed + 1


def test_the_share_cap_leaves_ordinary_traffic_to_the_window(client, monkeypatch) -> None:
    """A busy room whose ring is full must behave exactly as it did for a phrase nobody is
    repeating at scale: the window refuses the sixth copy and hands the phrase back once
    the window passes. The share cap is live throughout - DUPE_RING distinct messages
    landed first - and the arithmetic, not an off switch, is what keeps it quiet."""
    clock = {"now": 9000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["now"])
    with _filter_on():
        for i in range(limit.DUPE_RING):
            other = "a distinct message number " + str(i) + " from a real conversation"
            assert _say(client, "lobby", "n" + str(i), other).status_code == 200
        for i in range(COPIES):
            assert _say(client, "lobby", "m" + str(i), PHRASE).status_code == 200
        assert _say(client, "lobby", "m9", PHRASE).status_code == 422, "the window, as before"
        clock["now"] += WINDOW + 1
        assert _say(client, "lobby", "m8", PHRASE).status_code == 200, "and it still expires"


def test_a_write_the_store_refuses_never_spends_a_share_slot(client) -> None:
    """The ring slot is reserved before the append exactly as the window's timestamp is,
    so it has to be handed back the same way. Without that, a caller sending malformed
    writes of one phrase would leave a stranger's identical phrase refused against copies
    nothing ever stored - the leak test_a_write_the_store_refuses_never_spends_a_copy
    covers for the window, one signal over, and cross-sender for the same reason."""
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    opening = "a first message, so the room exists before the malformed writes arrive"
    with _filter_on():
        # The room is created first, deliberately: the write gate charges a room-creation
        # token per write to a room it cannot see, and that budget is measured in days, so
        # dozens of failed writes to an absent room meet a 429 long before this assertion.
        assert _say(client, "lobby", "nick", opening).status_code == 200
        for _ in range(allowed + 3):
            # Uppercase, which store.valid_name refuses - a 400 raised INSIDE the append,
            # after the slot for this text was already reserved.
            assert _say(client, "lobby", "Nick", PHRASE).status_code == 400
        assert _say(client, "lobby", "nick", PHRASE).status_code == 200
    assert _view(client) == [opening, PHRASE]
    # The ring is keyed by (room, incarnation) now; lobby has had exactly one, so sum its
    # slots across whatever generation it landed in rather than pinning the number here.
    held = sum(len(v) for (r, _), v in limit._rings.items() if r == "lobby")
    assert held == 2, "only the two writes that landed hold slots"


def test_a_reaped_and_recreated_room_starts_with_an_empty_share_ring(client, monkeypatch) -> None:
    """A room the store reaps and a caller later recreates under the same name must start
    with a clean share ring - the recreated room holds zero copies of any phrase, so its
    first copy of one is legitimate and must land.

    store._reap deletes the room file and preserves only its seq floor and generation; it
    never imports limit and cannot touch limit._rings. Before #734 the ring was keyed by
    BARE room name with no expiry, so the in-memory share count survived the reap and the
    recreated room inherited it: once that stale count was at the cap, a first, legitimate
    copy in the new room met a 422 nothing in the new room justified. Reviewers (yukkie3276,
    luch91) flagged it from the source, not a run. The fix keys the ring by room INCARNATION
    (store.room_generation), so the recreated room is a different key and the dead one is
    consulted by nothing. This test drives the REAL reaper end to end.
    """
    import store

    root = config.ROOT

    def _ring_of(name):  # the ring entries for a room across whatever generations it has had
        return {gen: slots for (r, gen), slots in limit._rings.items() if r == name}

    # A share cap of three copies, so the ring fills without spending a whole DUPE_RING and
    # the whole lifecycle stays inside one window and one budget.
    monkeypatch.setattr(limit, "DUPE_SHARE", 3 / limit.DUPE_RING)
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    assert allowed == 3
    with _filter_on():
        for i in range(allowed):
            assert _say(client, "room-x", "n" + str(i), PHRASE).status_code == 200
    # The ring now holds `allowed` copies of PHRASE for room-x's first incarnation; one more
    # would 422 - which is what a fresh room must NOT inherit.
    old_gen = store.room_generation(root, "room-x")
    assert _ring_of("room-x") == {old_gen: limit._rings[("room-x", old_gen)]}
    assert len(limit._rings[("room-x", old_gen)]) == allowed

    # Reap room-x for real: age its file past the idle threshold and run one pass.
    files = list(root.rglob("room-x.jsonl"))
    assert files, "room-x was never written to disk"
    for f in files:
        _client._age(f, store.IDLE_SECONDS + 60)
    (root / ".reaped").unlink(missing_ok=True)
    store._reap(root)
    assert not list(root.rglob("room-x.jsonl")), "the reaper did not delete room-x"
    # The reap deleted the room from disk but, as documented, did NOT touch the in-memory
    # ring - the stale slots are still there. The fix does not depend on clearing them; it
    # depends on the recreated room keying under a NEW generation, leaving these to LRU.
    assert len(limit._rings[("room-x", old_gen)]) == allowed, "reap must not touch the ring"

    # Recreate room-x under the same name: a fresh conversation, so the generation bumps.
    with _filter_on():
        assert (
            _say(client, "room-x", "fresh", "a brand new opening line for this room").status_code
            == 200
        )
        new_gen = store.room_generation(root, "room-x")
        assert new_gen > old_gen, "recreation must bump the generation"
        # The first copy of PHRASE in the recreated room. Nothing THIS room retains justifies
        # a refusal - the stale count sits under the dead incarnation, not this one.
        first_copy = _say(client, "room-x", "someone", PHRASE)
    assert first_copy.status_code == 200, (
        "a recreated room refused a first, legitimate copy against a stale share ring the "
        "reap left behind: " + first_copy.text[:200]
    )
    # The new incarnation counts PHRASE from zero (its own slot for this one copy), and the
    # dead incarnation's slots are untouched, sitting idle until MAX_RING_ROOMS evicts them.
    assert len(limit._rings[("room-x", new_gen)]) == 2, "the recreated room's own two writes"
    assert old_gen in _ring_of("room-x"), "the dead incarnation's ring is orphaned, not read"


def test_a_reap_between_a_reservation_and_its_write_cannot_orphan_the_slot(
    client, monkeypatch
) -> None:
    """yukkie3276's TOCTOU race (#734, at head 7ee9c4d). The reservation used to read the
    generation and the room's existence BEFORE store.append took the room lock, so a reap
    landing in that gap recreated the room at a NEW generation while the slot stayed keyed to
    the old one: the accepted copy stranded a generation back, counting against nothing, so
    the room's share cap ran one copy loose.

    The reservation is taken INSIDE the append now, under the room lock the append and the
    reaper share, keyed by the generation the write settles on - the seam the race needed is
    gone. Reproduced through the one read that used to make the prediction, app._room_exists:
    pinned True while the room is actually absent on disk is exactly what a reap in the gap
    leaves - the room LOOKS present (the old code predicted the old generation) but the write
    recreates it at generation+1. The lie changes nothing now; store reads the generation
    itself, under the lock. On the old code the first copy's slot orphaned under generation 0,
    the cap counted short, and the (allowed+1)th copy wrongly landed.
    """
    import app as app_module
    import store

    root = config.ROOT
    # A share cap of three, exactly as the reap/recreate test above, so the ring fills inside
    # one window and the share rule (not the looser window rule at COPIES) is what binds.
    monkeypatch.setattr(limit, "DUPE_SHARE", 3 / limit.DUPE_RING)
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    assert allowed == 3
    # Seen as existing at prediction time while every write recreates it - the reap in the gap.
    # The room starts absent, so the first write bumps its generation from 0 to 1 under the lock.
    monkeypatch.setattr(app_module, "_room_exists", lambda room: True)
    with _filter_on():
        outcomes = [
            _say(client, "toctou", "n" + str(i), PHRASE).status_code for i in range(allowed + 1)
        ]
    new_gen = store.room_generation(root, "toctou")
    assert new_gen == 1, "the first write recreated the room at generation 1"
    # Exactly `allowed` land and the next is refused: the cap binds under the generation the
    # writes actually live in. The old code stranded one slot a generation back, counted
    # `allowed - 1`, and let this last copy through with a 200.
    assert outcomes == [200] * allowed + [422], outcomes
    assert len(limit._rings[("toctou", new_gen)]) == allowed, "every landed copy in the live ring"
    assert ("toctou", 0) not in limit._rings, "nothing stranded under the pre-recreation generation"


def test_a_store_failure_after_the_reservation_hands_the_slot_back(client, monkeypatch) -> None:
    """The reservation is taken inside the append now, so a store failure AFTER it - a torn
    write, a disk error - must hand the slot back, the way the old pre-append reservation
    released on any append refusal. Otherwise a run of such failures would spend a room's
    window on a text nothing stored, and the next well-formed caller of that phrase would meet
    a 422 for copies that never landed.

    Fails at last_seq, the first store read after the reservation and before any byte is
    written, so nothing lands and only the release path runs. Driven through store.append
    directly - the failure raises, which the TestClient would re-raise through the HTTP lane.

    Each doomed call here is a create (the room never comes into existence), and the create
    now advances the generation as its precondition, ahead of the reservation and the append
    (Minh3132, #734) - so every failure burns one generation before failing. That is the
    benign skipped-generation the design accepts: monotonic, gapless-not-required, and the
    reservation still keys on whatever generation is actually durable. So the room settles a
    few generations on from 0, and the assertions read the live generation rather than assume
    it, and check nothing stayed reserved under the burned ones.
    """
    import app as app_module
    import store

    root = config.ROOT
    real_last_seq = store.last_seq

    def boom(r, room):
        if room == "boom":
            raise OSError("injected: the write failed after the slot was reserved")
        return real_last_seq(r, room)

    monkeypatch.setattr(store, "last_seq", boom)
    with _filter_on():
        for _ in range(COPIES + 2):  # more failures than the window would ever allow copies
            with pytest.raises(OSError):
                store.append(
                    root, "boom", "nick", PHRASE, reserve=app_module._reserver("boom", PHRASE)
                )
        monkeypatch.undo()
        # Nothing holds the phrase's window - every reserved slot was handed back - so COPIES
        # fresh copies land and only the (COPIES+1)th is the refusal the filter is actually for.
        for i in range(COPIES):
            assert _say(client, "boom", "n" + str(i), PHRASE).status_code == 200
        assert _say(client, "boom", "last", PHRASE).status_code == 422
    gen = store.room_generation(root, "boom")
    assert len(limit._rings[("boom", gen)]) == COPIES, "the copies that landed, and only those"
    assert sum(len(s) for (r, _), s in limit._rings.items() if r == "boom") == COPIES, (
        "nothing stayed reserved under the generations the failed creates burned"
    )


def test_a_compaction_failure_after_the_append_lands_keeps_the_slot(client, monkeypatch) -> None:
    """The mirror image of the release test above (Minh3132, #734 at head 5ffc86f). A failure
    BEFORE the record commits hands the slot back; a failure AFTER it must NOT. `_write_record`
    used to wrap the append and the follow-on `_compact` in one release-covered try, so a
    compaction I/O error on an over-limit room released a reservation whose record was already
    flushed to disk - the copy stayed stored but stopped counting against both the window and
    the ring, and a run of such failures would leak copies past the cap.

    The try now ends at the append's clean exit, and `_compact` runs outside it. So: an
    existing room (created=False - the realistic over-limit case, and the one that keeps the
    reservation's generation matching the room's), one copy of PHRASE whose append flushes and
    then fails in compaction, and a cap of one. The committed copy must hold its slot, so the
    NEXT identical copy is refused by the window rule. On the old code the release dropped the
    slot and this second copy wrongly landed while the first sat stored and uncounted.
    """
    import app as app_module
    import store

    root = config.ROOT
    with _filter_on(DUPE_MAX_COPIES=1):
        # Bring the room into existence first, with a different phrase, so the failing append
        # below is a plain write (created=False) and its reservation keys under the same
        # generation the room already sits at - not the create path, where the skipped
        # generation bump would be its own separate concern.
        opener = "an opening line for this room that is not the phrase under test at all"
        assert _say(client, "compactboom", "opener", opener).status_code == 200
        gen = store.room_generation(root, "compactboom")

        # From here every write to this room reports over-limit and compaction raises - the
        # append lands, then _compact fails.
        monkeypatch.setattr(store, "_ring_limit", lambda r: 0)

        def boom(path, cutoff=None, keep=0):
            raise OSError("injected: compaction failed after the record was written")

        monkeypatch.setattr(store, "_compact", boom)

        with pytest.raises(OSError):
            store.append(
                root,
                "compactboom",
                "nick",
                PHRASE,
                reserve=app_module._reserver("compactboom", PHRASE),
            )
        # The record committed despite the compaction failure: it is on disk and readable.
        assert PHRASE in _view(client, "compactboom"), "the flushed record must survive"

        monkeypatch.undo()  # restore real compaction for the follow-on write
        # The kept reservation means the room already holds its one allowed copy, so a second
        # identical copy from another sender is the refusal the filter is for. Under the old
        # release-on-compaction-failure this landed with a 200.
        second = _say(client, "compactboom", "other", PHRASE)
    assert second.status_code == 422, (
        "a copy committed before a compaction failure was not counted, so a second copy leaked "
        "past the cap: " + second.text[:200]
    )
    assert len(limit._rings[("compactboom", gen)]) == 2, (
        "the opener and the committed PHRASE copy each hold a slot; nothing was released"
    )


def test_a_create_whose_generation_write_fails_commits_nothing(client, monkeypatch) -> None:
    """Minh3132's fifth #734 finding, at head c0a2d32. The create path used to bump the
    generation AFTER flushing the record and swallow that write's own failure: the committed
    record and its reserved slot then sat at generation g+1 while the durable generation
    stayed g, so every later write keyed under g and never counted the stored copy against the
    live room's cap - an undercount, and a g+1 the next reap/recreate would collide with.

    The bump is the create's PRECONDITION now, before the reservation and before the append,
    and it propagates its failure rather than swallowing it. So there is no window: a
    generation that cannot be persisted commits no record and reserves no slot. This drives
    the exact failure - the create-path generation write raises - and asserts the room is left
    as if untouched: no record, no ring slot, generation still 0, room count not moved.
    """
    import app as app_module
    import store

    root = config.ROOT
    real_set = store._set_seq_entry

    def boom(r, room, floor=None, *, bump=False):
        if room == "genboom" and bump:  # the create-path precondition bump, and only it
            raise OSError("injected: the generation could not be persisted")
        return real_set(r, room, floor, bump=bump)

    rooms_before = store._count_rooms(root)[0]
    monkeypatch.setattr(store, "_set_seq_entry", boom)
    with _filter_on():
        with pytest.raises(OSError):
            store.append(
                root, "genboom", "nick", PHRASE, reserve=app_module._reserver("genboom", PHRASE)
            )
    # Nothing committed: no file, no record, generation never advanced off "never existed".
    assert not store.room_path(root, "genboom").exists(), "a failed create left a room file"
    assert store.room_generation(root, "genboom") == 0, "the generation advanced despite failing"
    assert not any(r == "genboom" for r, _ in limit._rings), "a slot was reserved and stranded"
    assert store._count_rooms(root)[0] == rooms_before, "the room-count reservation leaked"

    # And no poison for the next well-formed create: with the injection gone it bumps to 1,
    # keys its slot under that live generation, and counts normally.
    monkeypatch.undo()
    with _filter_on():
        assert _say(client, "genboom", "nick", PHRASE).status_code == 200
    gen = store.room_generation(root, "genboom")
    assert gen == 1, "the first real create bumps the generation to 1"
    assert len(limit._rings[("genboom", gen)]) == 1, "the committed copy counts under the live gen"


def test_a_created_rooms_first_copy_counts_under_the_generation_it_commits_at(client) -> None:
    """The positive half of Minh3132's finding: a create's committed copy must count against
    the room's cap, and it can only do so if the reserved slot's generation and the room's
    durable generation agree. They now settle together - the bump precedes the commit, so the
    reservation reads the real durable generation rather than predicting g+1 - so the first,
    room-creating copy of a phrase holds its slot under the same generation `room_generation`
    reports, and the very next identical copy is refused by it.
    """
    import store

    root = config.ROOT
    with _filter_on(DUPE_MAX_COPIES=1):
        # The room-creating write: created=True, so the generation bumps 0 -> 1 as its
        # precondition and the slot is reserved under that same 1.
        assert _say(client, "gencount", "opener", PHRASE).status_code == 200
        gen = store.room_generation(root, "gencount")
        assert gen == 1, "the creating write settled the room at generation 1"
        # The reservation and the durable generation agree: the only slot is under (room, 1),
        # nothing stranded under 0 (the pre-bump value the old prediction would have used) or 2.
        assert ("gencount", 0) not in limit._rings, "a slot stranded a generation behind"
        assert len(limit._rings[("gencount", gen)]) == 1, "the created copy counts under gen 1"
        # So the next identical copy from another sender is the refusal the cap is for - the
        # committed create was counted, not lost to a generation nothing else keys under.
        second = _say(client, "gencount", "other", PHRASE)
    assert second.status_code == 422, (
        "a created room's first committed copy was not counted, so a second leaked past the "
        "cap: " + second.text[:200]
    )
