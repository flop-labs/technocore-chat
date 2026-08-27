"""Run: uv run --group dev python -m pytest tests

Retry idempotency for the unsigned write lanes, and the two properties that decide whether
it is safe to turn on: it must answer a repeat with the message it repeats rather than an
error, and its cache must stay bounded under exactly the flood that makes retries likely.

Off by default (see config.DEDUP_SECONDS) — nothing distinguishes a retry from a caller
that meant it twice, so these enable it explicitly, which is also how an operator does.
"""

from __future__ import annotations

import _client
import pytest

import config
import limit

client = _client.client  # the shared TestClient fixture


def test_it_is_off_unless_an_operator_turns_it_on(client) -> None:
    """The default is the safety property: identical rapid writes are ordinary traffic
    here, so out of the box both land and nothing is collapsed."""
    assert config.DEDUP_SECONDS == 0
    for _ in range(2):
        client.get("/r/lobby/say/bot/same")
    assert [m["seq"] for m in client.get("/r/lobby?format=json").json()["messages"]] == [1, 2]


@pytest.mark.parametrize(
    ("dedup_seconds", "lands"), [(0, ["ok", "ok"]), (30, ["ok"])], ids=["off", "on"]
)
def test_what_a_genuine_repeat_costs_in_each_configuration(client, dedup_seconds, lands) -> None:
    """The trade, stated as a test in both directions, because it is the reason the default
    is off.

    This is NOT a retry: the caller saw its first 200 and meant to say the same thing
    again, which is ordinary for agents ("ok", "+1", "done"). Nothing in the request tells
    the two apart, so turning dedup on to collapse retries also collapses this — the second
    message never lands. Off, both land and a genuine retry duplicates instead.

    Neither column is free. An operator picks the failure they would rather have, and this
    is where each one is written down.
    """
    with config.override(DEDUP_SECONDS=dedup_seconds):
        first = client.get("/r/lobby/say/agent/ok?format=json")
        second = client.get("/r/lobby/say/agent/ok?format=json")
    assert first.status_code == second.status_code == 200  # either way the caller sees 200
    assert [m["text"] for m in client.get("/r/lobby?format=json").json()["messages"]] == lands
    # With dedup on, the second call is answered with the first message's seq rather than
    # refused — the caller is never told its write failed.
    assert (first.json()["posted"]["seq"] == second.json()["posted"]["seq"]) is (dedup_seconds > 0)


def test_a_repeat_is_answered_with_the_message_it_repeats(client) -> None:
    """A retry means the caller never saw its 200. It gets the seq its message actually
    has — not an error, which would report failure for a write that succeeded."""
    with config.override(DEDUP_SECONDS=30):
        first = client.get("/r/lobby/say/bot/hello?format=json")
        again = client.get("/r/lobby/say/bot/hello?format=json")
    assert first.status_code == again.status_code == 200
    assert first.json()["posted"]["seq"] == again.json()["posted"]["seq"]
    assert first.json()["posted"]["ts"] == again.json()["posted"]["ts"]
    # …and the room holds one message, which is the point.
    msgs = client.get("/r/lobby?format=json").json()["messages"]
    assert [m["text"] for m in msgs] == ["hello"]


def test_only_an_identical_write_is_treated_as_a_repeat(client) -> None:
    """Every component of the key has to matter, or the dedup swallows other messages.
    Same text under a different nick, and a different text, are different writes."""
    with config.override(DEDUP_SECONDS=30):
        client.get("/r/lobby/say/alice/ok")
        client.get("/r/lobby/say/alice/ok")  # the repeat
        client.get("/r/lobby/say/bob/ok")  # same text, another agent
        client.get("/r/lobby/say/alice/done")  # same agent, another message
        client.get("/r/other/say/alice/ok")  # same write, another room
    lobby = client.get("/r/lobby?format=json").json()["messages"]
    assert [(m["from"], m["text"]) for m in lobby] == [
        ("alice", "ok"),
        ("bob", "ok"),
        ("alice", "done"),
    ]
    assert len(client.get("/r/other?format=json").json()["messages"]) == 1


def test_the_post_lane_and_the_get_lane_share_one_window(client) -> None:
    """Both unsigned lanes write the same record, so a caller that retries a dropped GET
    over POST is still retrying the same message."""
    with config.override(DEDUP_SECONDS=30):
        first = client.get("/r/lobby/say/bot/hey?format=json")
        again = client.post("/r/lobby?format=json", json={"from": "bot", "text": "hey"})
    assert first.json()["posted"]["seq"] == again.json()["posted"]["seq"]
    assert len(client.get("/r/lobby?format=json").json()["messages"]) == 1


def test_the_signed_lane_is_never_deduplicated(client) -> None:
    """A signed URL is single-use and the nonce refuses a replay. Answering one with 200
    and a seq would turn a security refusal into an acknowledgement, so the signed lane
    does not go near this."""
    did, sign = _client._keypair()
    with config.override(DEDUP_SECONDS=30):
        url = f"/r/lobby/say-signed/{did}/{sign('lobby|1|signed')}/1/signed"
        assert client.get(url).status_code == 200
        replay = client.get(url)
    # Refused by the nonce, which is a StoreError and so a 400. What matters is that it is
    # a refusal at all: a dedup hit would have been a 200 carrying a seq, which is the
    # difference between "your replay was rejected" and "your replay was accepted".
    assert replay.status_code == 400, f"a replayed signed URL must still be refused: {replay.text}"
    assert "single-use" in replay.text
    assert len(client.get("/r/lobby?format=json").json()["messages"]) == 1


# --------------------------------------------------------------------------- the bound


def test_the_window_closes(monkeypatch) -> None:
    """`now` is passed in rather than read, so expiry is testable without sleeping."""
    limit._recent.clear()
    limit.remember_write(("k",), 7, now=100.0, ttl=5.0, cap=64)
    assert limit.recent_write(("k",), now=104.9, ttl=5.0) == 7
    assert limit.recent_write(("k",), now=105.1, ttl=5.0) is None
    assert ("k",) not in limit._recent, "an expired entry must not be left behind"


def test_the_cache_stays_bounded_under_a_flood() -> None:
    """The requirement that matters under load. Every entry here is live — none has
    expired, so the sweep can free nothing and only the hard cap holds the line. A map
    that grows with traffic is worse than the duplicates it prevents.
    """
    limit._recent.clear()
    cap = 128
    for i in range(20_000):  # far past the cap, all within one window
        limit.remember_write((i,), i, now=1000.0, ttl=300.0, cap=cap)
    assert len(limit._recent) == cap

    # …and what survived is the newest, so the entries most likely to still be retried are
    # the ones kept. Eviction costs a duplicate, never a wrong answer.
    assert limit.recent_write((19_999,), now=1000.0, ttl=300.0) == 19_999
    assert limit.recent_write((0,), now=1000.0, ttl=300.0) is None


def test_one_write_never_pays_for_the_whole_backlog() -> None:
    """The sweep is capped per call, so a burst of expiry cannot turn a single write into
    a long pause — the thing this feature must not do is make everyone wait."""
    limit._recent.clear()
    for i in range(1000):
        limit.remember_write((i,), i, now=0.0, ttl=1.0, cap=10_000)
    before = len(limit._recent)
    limit.remember_write(("fresh",), 1, now=500.0, ttl=1.0, cap=10_000)  # all 1000 expired
    # 8 swept plus the one added: bounded work, with the rest left to later calls and to
    # the cap. Freeing all 1000 in one call is exactly the pause being avoided.
    assert len(limit._recent) == before - 8 + 1
