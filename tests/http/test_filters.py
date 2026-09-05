"""Run: uv run --group dev python -m pytest tests"""

import threading
import time

import _client
from _client import _keypair, _say_signed

import store

client = _client.client  # the shared TestClient fixture


def _seed(client):
    """Five lines: ~alice, A, B, ~alice, A — two verified writers around the unsigned lane."""
    did_a, sign_a = _keypair(1)
    did_b, sign_b = _keypair(2)
    client.get("/r/lobby/say/alice/one")
    _say_signed(client, "lobby", did_a, sign_a, "two", nonce=1)
    _say_signed(client, "lobby", did_b, sign_b, "three", nonce=1)
    client.get("/r/lobby/say/alice/four")
    _say_signed(client, "lobby", did_a, sign_a, "five", nonce=2)
    return did_a, sign_a, did_b


def test_from_keeps_one_signer_oldest_first(client):
    did_a, _, did_b = _seed(client)
    view = client.get(f"/r/lobby?from={did_a}&format=json").json()
    assert [(m["seq"], m["text"]) for m in view["messages"]] == [(2, "two"), (5, "five")]
    # first_seq/last_seq describe the scan (1..5), not the two lines shown
    assert view["count"] == 2 and view["first_seq"] == 1 and view["last_seq"] == 5
    assert {m["from"] for m in view["messages"]} == {did_a}
    body = client.get(f"/r/lobby?from={did_b}").text
    assert "three" in body and "two" not in body and "four" not in body


def test_a_nickname_cannot_be_filtered(client):
    _seed(client)
    # alice wrote 1 and 4, but a nick proves nothing: `from=` names verified writers only
    view = client.get("/r/lobby?from=alice&format=json").json()
    assert view["messages"] == [] and view["count"] == 0
    assert client.get("/r/lobby?from=did:key:z6MkNotAKey&format=json").json()["count"] == 0
    assert client.get("/r/lobby?from=&format=json").json()["count"] == 0


def test_signed_drops_the_unsigned_lane(client):
    _seed(client)
    view = client.get("/r/lobby?signed=1&format=json").json()
    assert [m["seq"] for m in view["messages"]] == [2, 3, 5]
    assert "~alice" not in client.get("/r/lobby?signed=1").text
    # only the documented value is the switch
    assert client.get("/r/lobby?signed=yes&format=json").json()["count"] == 5


def test_filters_compose_with_since_and_limit(client):
    did_a, _, _ = _seed(client)
    view = client.get(f"/r/lobby?from={did_a}&since=2&format=json").json()
    assert [m["seq"] for m in view["messages"]] == [5]
    # limit= is the newest N *matching* lines, exactly as it is the newest N without a
    # filter. A's newest line is seq 5, so a non-matching line on top of it is what tells
    # "newest matching" apart from "newest, then filtered" — without seq 6 here, a scan
    # that stopped at the newest raw record still returned [5] and passed.
    client.get("/r/lobby/say/alice/six")
    view = client.get(f"/r/lobby?from={did_a}&limit=1&format=json").json()
    assert [m["seq"] for m in view["messages"]] == [5] and view["first_seq"] == 5
    assert client.get("/r/lobby?signed=1&since=3&limit=1&format=json").json()["count"] == 1


def test_filtered_cursor_advances_past_lines_it_was_not_shown(client):
    did_a, _, _ = _seed(client)
    client.get("/r/lobby/say/bob/six")
    client.get("/r/lobby/say/bob/seven")
    # first_seq/last_seq describe the scan: 6..7 were seen, none matched, nothing was lost
    view = client.get(f"/r/lobby?from={did_a}&since=5&format=json").json()
    assert view["messages"] == [] and view["first_seq"] == 6 and view["last_seq"] == 7
    # the footer keeps the collection: a caller following `next:` stays on the same filter,
    # even one that matches nothing, and a caller-chosen value never reaches the line raw
    assert (
        f"next: /r/lobby?since=7&from={did_a}\n"
        in client.get(f"/r/lobby?from={did_a}&since=5").text
    )
    assert "next: /r/lobby?since=7&signed=1\n" in client.get("/r/lobby?signed=1&since=5").text
    assert "next: /r/lobby?since=7&from=alice&signed=1\n" in (
        client.get("/r/lobby?from=alice&signed=1&since=5").text
    )
    assert (
        "next: /r/lobby?since=7&from=a%20b%0Ac\n"
        in client.get("/r/lobby?from=a%20b%0Ac&since=5").text
    )
    assert "next: /r/lobby?since=7\n" in client.get("/r/lobby?since=5").text
    # an unfiltered read is unchanged: last_seq is the newest line returned, or `since`
    assert client.get("/r/lobby?since=5&format=json").json()["last_seq"] == 7
    assert client.get("/r/lobby?since=9&format=json").json()["last_seq"] == 9
    assert client.get("/r/empty-room?format=json").json()["last_seq"] == 0


class _Polls:
    """Gate the long-poll's reads so a test releases them one at a time, no timing.

    Only filtered reads are gated: the first (the route's own read) passes, and every poll
    inside `_await_messages` then blocks until `release()`; a write's reply read is not. `seen` counts polls that returned, so a
    test can wait for the poll it released to have run before it acts on the result.
    """

    def __init__(self, monkeypatch):
        import app as app_module

        self.calls, self.seen, self.open = 0, 0, False
        self.gate = threading.Semaphore(0)
        real = app_module.store.read_messages

        def gated(*args, **kwargs):
            if kwargs.get("keep") is None:  # a write's own reply read, or an unfiltered read
                return real(*args, **kwargs)
            self.calls += 1
            if self.calls > 1 and not self.open:
                assert self.gate.acquire(timeout=10), "a poll was never released"
            out = real(*args, **kwargs)
            if self.calls > 1:
                self.seen += 1
            return out

        monkeypatch.setattr(app_module.store, "read_messages", gated)
        monkeypatch.setattr(app_module, "WAIT_POLL", 0.001)

    def open_all(self):
        """Stop gating: the remaining polls run freely (to let a wait run out its clock)."""
        self.open = True
        self.gate.release()

    def release(self, n=1):
        """Let `n` polls run and wait until they have."""
        target = self.seen + n
        for _ in range(n):
            self.gate.release()
        for _ in range(2000):
            if self.seen >= target:
                return
            time.sleep(0.005)
        raise AssertionError("the released poll never ran")


def _waiting(client, url, monkeypatch):
    """Issue a long-poll on a thread and block until it holds a waiter slot."""
    import app as app_module

    polls = _Polls(monkeypatch)
    box = {}

    def go():
        box["view"] = client.get(url).json()

    t = threading.Thread(target=go)
    t.start()
    for _ in range(2000):
        if app_module._waiters_total:
            break
        time.sleep(0.005)
    assert app_module._waiters_total, "the waiter never took a slot"
    return t, box, polls


def test_wait_ends_on_a_matching_line_not_on_any_line(client, monkeypatch):
    did_a, sign_a, _ = _seed(client)
    t, box, polls = _waiting(
        client, f"/r/lobby?from={did_a}&since=5&wait=10&format=json", monkeypatch
    )
    client.get("/r/lobby/say/bob/noise")  # must NOT end the wait
    polls.release()
    assert t.is_alive(), "the wait ended on a line the filter drops"
    _say_signed(client, "lobby", did_a, sign_a, "signal", nonce=3)
    polls.release()
    t.join(timeout=10)
    assert [m["text"] for m in box["view"]["messages"]] == ["signal"]
    # the reply describes the whole wait: the noise at 6 was scanned, nothing was lost
    assert box["view"]["first_seq"] == 6 and box["view"]["last_seq"] == 7


def test_a_timed_out_filtered_wait_hands_back_the_advanced_cursor(client, monkeypatch):
    did_a, _, _ = _seed(client)
    t, box, polls = _waiting(
        client, f"/r/lobby?from={did_a}&since=5&wait=0.3&format=json", monkeypatch
    )
    client.get("/r/lobby/say/bob/noise")
    polls.release()
    polls.open_all()  # let the remaining polls run out the clock
    t.join(timeout=10)
    # nothing matched, but the wait scanned line 6 and says so instead of returning to 5
    assert box["view"]["messages"] == [] and (
        box["view"]["first_seq"],
        box["view"]["last_seq"],
    ) == (6, 6)
    # without a filter an empty wait is unchanged: last_seq is the caller's since
    assert client.get("/r/lobby?since=6&wait=0.05&format=json").json()["last_seq"] == 6


def test_an_advanced_cursor_and_a_held_wait_are_reported_together(client, monkeypatch):
    """The two halves of an empty filtered wait answer different questions and must both
    survive: `wait_held` says the service really waited (it is False only when no waiter
    slot was free), and the cursor says how far the wait scanned. A filtered wait that ends
    empty comes back as a view rather than as None, so the flag cannot be keyed on that."""
    did_a, _, _ = _seed(client)
    t, box, polls = _waiting(
        client, f"/r/lobby?from={did_a}&since=5&wait=0.3&format=json", monkeypatch
    )
    client.get("/r/lobby/say/bob/noise")
    polls.release()
    polls.open_all()
    t.join(timeout=10)
    assert box["view"]["wait_held"] is True and box["view"]["last_seq"] == 6

    # …and a wait that produced a match stays bare: the flag is advice for an empty answer.
    view = client.get(f"/r/lobby?from={did_a}&since=1&wait=0.05&format=json").json()
    assert view["messages"] and "wait_held" not in view


def test_an_empty_filtered_wait_reports_the_generation_it_scanned(client, monkeypatch):
    """`generation` and `last_seq` have to come from the same scan. A wait describes the
    newest poll it made, not the view it started from: otherwise a cursor that advanced
    into a new epoch is handed back stamped with the old one, and `generation` is exactly
    the field a caller reads to notice the epoch changed under it."""
    did_a, _, _ = _seed(client)
    before = client.get("/r/fresh-room?format=json").json()
    assert before["generation"] == 0  # the room does not exist yet
    t, box, polls = _waiting(
        client, f"/r/fresh-room?from={did_a}&since=0&wait=0.3&format=json", monkeypatch
    )
    client.get("/r/fresh-room/say/bob/creates-the-room")  # a non-matching line, generation 1
    polls.release()
    polls.open_all()
    t.join(timeout=10)
    view = box["view"]
    assert view["messages"] == [] and view["last_seq"] == 1
    assert view["generation"] == client.get("/r/fresh-room?format=json").json()["generation"] == 1


def test_a_wait_that_crosses_an_epoch_ends_there_instead_of_spanning_both(client, monkeypatch):
    """A room reaped and recreated under a waiter is what `generation` exists to announce,
    so the poll that first sees the new epoch is returned whole. Carrying the earlier scan's
    `first_seq` into it would hand back one view describing two conversations, and
    `first_seq > since + 1` — "lines you never saw" — is not a question that spans them."""
    import asyncio
    from types import SimpleNamespace
    from typing import cast

    from starlette.requests import Request

    import app as app_module

    did_a, _, _ = _seed(client)
    client.get("/r/lobby/say/bob/six")  # unmatched, so the pre-wait scan carries first_seq 6
    stale = client.get(f"/r/lobby?from={did_a}&since=5&format=json").json()
    assert (stale["messages"], stale["first_seq"], stale["generation"]) == ([], 6, 1)

    # the same name, one epoch on, carrying a line the old cursor never described
    client.get("/r/lobby/say/bob/seven")
    reborn = {
        **app_module.store.read_messages(app_module.config.ROOT, "lobby", since=6),
        "generation": 2,
    }
    assert reborn["first_seq"] == 7 and reborn["messages"]
    monkeypatch.setattr(app_module.store, "read_messages", lambda *a, **k: reborn)
    monkeypatch.setattr(app_module, "WAIT_POLL", 0)

    class Caller:
        headers = {}
        client = SimpleNamespace(host="203.0.113.9")

        async def is_disconnected(self):
            return False

    got, note = asyncio.run(
        app_module._await_messages(cast(Request, Caller()), "lobby", 50, stale, 10, lambda m: True)
    )
    # the new epoch's own scan, verbatim — not it spliced onto the old scan's first_seq 6
    assert (got, note) == (reborn, "") and got is not None
    assert got["first_seq"] == 7


def _truncate_scans(monkeypatch, max_bytes=4096):
    import functools

    import store

    monkeypatch.setattr(
        store, "reverse_lines", functools.partial(store.reverse_lines, max_bytes=max_bytes)
    )


def test_a_truncated_filtered_scan_still_reports_the_lines_it_never_saw(client, monkeypatch):
    did_a, _, _ = _seed(client)  # A's line 2 is the only match, at the bottom
    for _ in range(12):
        client.get(f"/r/lobby/say/bob/{'x' * 500}")
    _truncate_scans(monkeypatch)
    view = client.get(f"/r/lobby?from={did_a}&since=2&format=json").json()
    # the budget ran out before the scan reached since=2: no match, but first_seq says so
    assert view["messages"] == [] and view["first_seq"] > 3 and view["last_seq"] == 17
    # the same signal an unfiltered read gives for the same truncation
    plain = client.get("/r/lobby?since=2&format=json").json()
    assert plain["first_seq"] == view["first_seq"] and plain["last_seq"] == 17


def test_a_timed_out_wait_keeps_the_gap_its_first_scan_reported(client, monkeypatch):
    did_a, _, _ = _seed(client)
    for _ in range(12):
        client.get(f"/r/lobby/say/bob/{'x' * 500}")
    _truncate_scans(monkeypatch)
    gap = client.get(f"/r/lobby?from={did_a}&since=2&format=json").json()["first_seq"]
    t, box, polls = _waiting(
        client, f"/r/lobby?from={did_a}&since=2&wait=0.3&format=json", monkeypatch
    )
    client.get("/r/lobby/say/bob/noise")
    polls.release()
    polls.open_all()
    t.join(timeout=10)
    # the cursor moved past the noise, and the warning about seqs 3..gap-1 is still there
    assert box["view"]["messages"] == [] and box["view"]["last_seq"] == 18
    assert box["view"]["first_seq"] == gap > 3


def test_a_wait_ends_early_when_a_poll_itself_runs_out_of_budget(client, monkeypatch):
    did_a, _, _ = _seed(client)
    _truncate_scans(monkeypatch)
    t, box, polls = _waiting(
        client, f"/r/lobby?from={did_a}&since=5&wait=10&format=json", monkeypatch
    )
    for _ in range(12):
        client.get(f"/r/lobby/say/bob/{'x' * 500}")
    polls.release()  # one poll, and it sees the whole burst
    t.join(timeout=10)
    # more arrived than one scan can cover: the poll reports the gap now, instead of moving
    # the cursor past lines nobody looked at and waiting on
    view = box["view"]
    assert view["messages"] == [] and view["last_seq"] == 17 and view["first_seq"] > 6


def test_a_record_without_a_writer_does_not_break_a_filtered_read(client, tmp_path):
    did_a, _, _ = _seed(client)
    # the store shards room files one level, so reach the file the reader actually scans
    with store.room_path(tmp_path, "lobby").open("ab") as f:
        f.write(b'{"seq":6,"ts":"2026-01-01T00:00:00.000000Z","text":"restored by hand"}\n')
    # last_seq is the newest line *scanned*, so it is the one assertion that fails if the
    # record ever stops being reachable — the counts below are already true without it, and
    # a rebase that moved the room file once made this test pass while scanning nothing.
    assert client.get("/r/lobby?signed=1&format=json").json()["last_seq"] == 6
    assert client.get("/r/lobby?signed=1&format=json").json()["count"] == 3
    assert client.get("/r/lobby?from=did:key:z6MkNotAKey&format=json").json()["count"] == 0

    # A filtered ?format=json read must pick its lane before anything renders: a kept record
    # missing a field only the text lane touches is JSON the caller can still have.
    with store.room_path(tmp_path, "lobby").open("ab") as f:
        f.write(b'{"seq":7,"from":"' + did_a.encode() + b'"}\n')
    assert client.get("/r/lobby?signed=1&format=json").status_code == 200


def test_filters_are_documented(client):
    params = client.get("/openapi.json").json()["paths"]["/r/{room}"]["get"]["parameters"]
    assert {"from", "signed"} <= {p["name"] for p in params}
    manual = client.get("/llms.txt").text
    assert "?from=<did:key...>" in manual and "FILTERS:" in manual
    paths = {c["path"] for c in client.get("/.well-known/agent.json").json()["capabilities"]}
    assert "/r/{room}?from={did}" in paths
