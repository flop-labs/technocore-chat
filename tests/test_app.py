"""Run: uv run --group dev python -m pytest tests"""

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    os.environ["CHAT_ROOT"] = str(tmp_path)
    for mod in ("app", "store"):
        sys.modules.pop(mod, None)
    import app as app_module

    return TestClient(app_module.app)


def test_say_then_read(client):
    r = client.get("/r/lobby/say/alice/hello%20world")
    # `~alice`, not `alice`: an unsigned nick is self-asserted and the text view says so
    assert r.status_code == 200 and "<~alice> hello world" in r.text
    body = client.get("/r/lobby").text
    assert "[1]" in body and "hello world" in body
    assert "UNTRUSTED CONTENT" in body  # injection framing always present


def test_since_cursor_returns_only_new(client):
    for i in range(3):
        client.get(f"/r/lobby/say/bot/msg{i}")
    view = client.get("/r/lobby?since=2&format=json").json()
    assert [m["seq"] for m in view["messages"]] == [3]
    assert client.get("/r/lobby?since=3&format=json").json()["count"] == 0


def test_traversal_and_bad_names_rejected(client, tmp_path):
    assert client.get("/r/..%2F..%2Fetc/say/x/y").status_code in (400, 404)
    assert client.get("/r/UPPER/say/x/y").status_code == 400
    assert client.get("/kv/..%2F..%2Fetc/passwd/set/x").status_code in (400, 404)
    assert not (tmp_path / "rooms" / "UPPER.jsonl").exists()
    assert list(tmp_path.rglob("*")) == [] or all(p.name != "passwd" for p in tmp_path.rglob("*"))
    # a slash inside <nick> splits path segments, it never nests a directory
    client.get("/r/lobby/say/n%2Fick/y")
    assert client.get("/r/lobby?format=json").json()["messages"][0]["from"] == "n"


def test_notes_roundtrip(client):
    assert client.get("/kv/plans/next/set/ship%20the%20thing").status_code == 200
    assert "ship the thing" in client.get("/kv/plans/next").text
    assert "/kv/plans/next" in client.get("/kv/plans").text
    assert client.get("/kv/plans/missing").status_code == 404


def test_post_lane(client):
    r = client.post("/r/lobby", json={"from": "carol", "text": "via post"})
    assert r.status_code == 200 and "via post" in r.text
    assert client.post("/r/lobby", content=b"x" * 40_000).status_code == 413


def test_control_chars_cannot_forge_records(client):
    # the say route's path regex never matches a raw newline: request is dropped
    assert client.get("/r/lobby/say/mallory/a%0A%7B%22seq%22%3A99%7D").status_code == 404
    # the POST lane accepts newlines, and flattens them so one message stays one line
    client.post("/r/lobby", json={"from": "mallory", "text": 'a\n{"seq":99,"from":"admin"}'})
    view = client.get("/r/lobby?format=json").json()
    assert view["count"] == 1
    assert view["messages"][0]["seq"] == 1 and view["messages"][0]["from"] == "mallory"


def test_compaction_bounds_file_and_keeps_seq(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 4096)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 2048)
    for _ in range(200):
        store.append(tmp_path, "big", "bot", "x" * 100)
    path = store.room_path(tmp_path, "big")
    assert path.stat().st_size <= 4096
    view = store.read_messages(tmp_path, "big", limit=50)
    assert view["last_seq"] == 200 and view["first_seq"] > 1  # gap is observable


def test_private_names_are_reachable_but_never_enumerated(client):
    client.get("/r/p-7f3a9c/say/bot/secret%20journal")
    client.get("/kv/p-7f3a9c/state/set/step%3D4")
    client.get("/kv/plans/p-draft/set/wip")
    assert "secret journal" in client.get("/r/p-7f3a9c").text  # readable if you know it
    assert "step=4" in client.get("/kv/p-7f3a9c/state").text
    assert "p-7f3a9c" not in client.get("/rooms").text  # but absent from listings
    assert "p-draft" not in client.get("/kv/plans").text


def test_rate_limit_is_actionable_without_headers(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "RATE_WRITE", 4)
    codes = [client.get(f"/r/lobby/say/bot/m{i}").status_code for i in range(6)]
    assert codes[:4] == [200] * 4 and codes[4:] == [429, 429]
    r = client.get("/r/lobby/say/bot/again")
    assert r.headers["retry-after"].isdigit()
    # the wait is in the body too: harness webfetch shows page text, not headers
    assert "retry after:" in r.text and "429 rate limited" in r.text
    # the manual stays reachable while throttled, so a limited agent can learn to back off
    assert client.get("/llms.txt").status_code == 200
    assert client.get("/r/lobby").status_code == 200  # reads have their own budget


def test_the_429_names_the_budget_the_manual_deliberately_does_not(client, monkeypatch):
    """The numbers are per deployment, so no document states them as prose. That only works
    if the responses carry them — otherwise removing them from the manual just loses them.
    """
    import app as app_module

    monkeypatch.setattr(app_module, "RATE_WRITE", 2)
    for i in range(3):
        r = client.get(f"/r/lobby/say/bot/m{i}")
    assert r.status_code == 429
    assert "(2/min)" in r.text  # the enforced number, not a documented one
    assert "one token every 30s" in r.text  # …and the refill, as a sleep
    # what still works while throttled, and where to read the limits up front
    assert "reads are a separate budget" in r.text
    assert "limits.writes_per_minute_per_ip" in r.text


def test_the_refill_rate_stays_a_number_an_agent_can_pace_against(client):
    """`{per_min/60:.1f} tokens/s` prints a flat "0.0 tokens/s" below 30/min — useless on
    exactly the deployments that throttle hardest. Under 1/s the period is the useful form.
    """
    import app as app_module

    assert app_module.refill_rate(120) == "2.0 tokens/s"
    assert app_module.refill_rate(60) == "1.0 tokens/s"
    assert app_module.refill_rate(30) == "one token every 2s"
    assert app_module.refill_rate(1) == "one token every 60s"


def test_a_zero_rate_limit_refuses_rather_than_crashing(monkeypatch, tmp_path):
    """The bucket arithmetic divides by the limit, so CHAT_RATE_WRITE=0 turned every write
    into a 500 on the limiter itself. Floored at import instead."""
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    monkeypatch.setenv("CHAT_RATE_WRITE", "0")
    for mod in ("app", "store"):
        sys.modules.pop(mod, None)
    import app as app_module

    assert app_module.RATE_WRITE == 1
    assert TestClient(app_module.app).get("/r/lobby/say/bot/hi").status_code == 200


def test_every_path_the_429_calls_free_really_is_free(client, monkeypatch):
    """Advice that fails at the moment it is taken is worse than no advice: a throttled
    agent following this list must not meet a second 429."""
    import app as app_module

    monkeypatch.setattr(app_module, "RATE_READ", 1)
    client.get("/rooms")
    assert client.get("/rooms").status_code == 429  # the budget really is spent

    named = app_module.FREE_PATHS.replace(" and ", ", ").split(", ")
    concrete = {
        "/.well-known/*": [
            "/.well-known/agent.json",
            "/.well-known/api-catalog",
            "/.well-known/ai-catalog.json",
            "/.well-known/agent-skills/index.json",
        ]
    }
    paths = [p for name in named for p in concrete.get(name.strip(), [name.strip()])]
    assert len(paths) >= 8
    for path in paths:
        assert client.get(path).status_code == 200, f"{path} is advertised as free but is not"


def test_budget_warning_appears_before_the_wall(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "RATE_READ", 8)
    assert "# budget:" not in client.get("/r/lobby").text
    for _ in range(5):
        client.get("/r/lobby")
    assert "# budget: 1 of 8 reads left" in client.get("/r/lobby").text


def test_room_count_is_capped_so_disk_is_bounded(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 3)
    # The events room is a real room on disk and counts against the cap — it costs one
    # slot, once, on the first public room created.
    store.append(tmp_path, "room0", "bot", "hi")  # creates room0 AND events -> 2
    store.append(tmp_path, "room1", "bot", "hi")  # -> 3, at the cap
    store.append(tmp_path, "room1", "bot", "still fine")  # existing rooms keep working
    with pytest.raises(store.StoreError, match="room limit"):
        store.append(tmp_path, "overflow", "bot", "hi")


def test_junk_query_params_never_500(client):
    """A harness that mangles a URL must get the default view, not a stack trace."""
    client.get("/r/lobby/say/bot/hi")
    for q in ("since=²", "limit=²", "since=abc", "limit=-3", "since=", "limit=1e3"):
        r = client.get(f"/r/lobby?{q}")
        assert r.status_code == 200, q
        assert "hi" in r.text, q


def test_rejected_write_leaves_no_lock_file(tmp_path, monkeypatch):
    """A cap that spends an inode per rejection is not a cap."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 1)
    store.append(tmp_path, "only", "bot", "hi")
    for i in range(5):
        with pytest.raises(store.StoreError, match="room limit"):
            store.append(tmp_path, f"flood{i}", "bot", "hi")
    assert list((tmp_path / "rooms").glob("*.lock")) == [
        store.room_path(tmp_path, "only").with_suffix(".jsonl.lock")
    ]


def test_notes_are_capped_across_namespaces(tmp_path, monkeypatch):
    """Rotating the namespace must not buy unbounded disk: the global cap binds."""
    import store

    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", 3)
    for i in range(3):
        store.note_set(tmp_path, f"ns{i}", "k", "v")  # a fresh namespace each time
    store.note_set(tmp_path, "ns1", "k", "v2")  # overwriting an existing note still works
    with pytest.raises(store.StoreError, match="across all namespaces"):
        store.note_set(tmp_path, "ns-fresh", "k", "v")
    assert not (tmp_path / "notes" / "ns-fresh").exists()  # rejection creates no namespace


def test_note_cap_holds_under_concurrent_creates(tmp_path, monkeypatch):
    """Sync handlers run in a threadpool: a cap counted across files needs one gate.

    Per-key locks let N concurrent creates each count `cap - 1` and each write, so the
    documented hard cap would be soft by up to one note per in-flight request.
    """
    import threading

    import store

    monkeypatch.setattr(store, "MAX_NOTES_TOTAL", 4)
    real_check = store._check_note_capacity

    def slow_check(root, path):
        real_check(root, path)
        time.sleep(0.02)  # widen the count→write window every racer must lose

    monkeypatch.setattr(store, "_check_note_capacity", slow_check)
    start = threading.Barrier(8)

    def create(i):
        start.wait()
        try:
            store.note_set(tmp_path, f"ns{i}", "k", "v")
        except store.StoreError:
            pass

    threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for _ in (tmp_path / "notes").glob("*/*.txt")) == 4


def test_orphan_locks_are_swept(tmp_path):
    import store

    store.append(tmp_path, "gone", "bot", "hi")
    path = store.room_path(tmp_path, "gone")
    lock = path.with_suffix(".jsonl.lock")
    assert lock.exists()
    old = time.time() - store.IDLE_SECONDS - 60
    for p in (path, lock):
        os.utime(p, (old, old))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store.append(tmp_path, "other", "bot", "hi")  # reaps the data file, keeps its lock
    assert not path.exists()
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store.append(tmp_path, "other", "bot", "again")  # next pass sweeps the orphan lock
    assert not lock.exists()


def test_reap_keeps_a_file_refreshed_after_the_stat(tmp_path, monkeypatch):
    """The reaper must recheck mtime under the lock, or it deletes live messages."""
    import store

    store.append(tmp_path, "live", "bot", "hi")
    path = store.room_path(tmp_path, "live")
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(path, (old, old))
    real_locked = store._locked

    @contextmanager
    def refresh_then_lock(target):
        with real_locked(target):
            os.utime(target, None)  # a writer got in between the stat and the unlink
            yield

    monkeypatch.setattr(store, "_locked", refresh_then_lock)
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert path.exists()


def test_rate_limit_buckets_are_bounded(client, monkeypatch):
    """Every unseen IP adds entries; unbounded, a rotating-IP flood OOMs the container."""
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_BUCKETS", 8)
    # Opted in explicitly: no forwarded header is trusted by default, so without this the
    # whole loop is one client (the test socket) and nothing rotates.
    monkeypatch.setattr(app_module, "CLIENT_IP_HEADER", "cf-connecting-ip")
    for i in range(50):
        client.get("/r/lobby", headers={"cf-connecting-ip": f"2001:db8::{i:x}"})
    assert len(app_module._buckets) <= 8
    # the survivors are the most recent callers, so an active client keeps its budget
    assert ("2001:db8::31", "read") in app_module._buckets


def test_no_forwarded_header_is_trusted_by_default(client, monkeypatch):
    """A forwarded-for header is a claim by the client. Trusting one unconditionally let
    anyone who could reach the origin directly mint a fresh rate-limit identity per request
    — which is every self-hoster who runs the image without locking it to a proxy."""
    import app as app_module

    assert app_module.CLIENT_IP_HEADER == ""
    spoofed = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "198.51.100.7"}
    before = set(app_module._buckets)
    client.get("/r/lobby", headers=spoofed)
    client.get("/r/lobby", headers={"cf-connecting-ip": "203.0.113.10"})
    # both requests land in the socket peer's bucket, not two attacker-chosen ones
    assert not {k for k in app_module._buckets if k[0].startswith(("203.0.113", "198.51.100"))}
    assert set(app_module._buckets) - before  # the peer's own bucket did get created

    # an operator whose origin really is locked to a proxy can still opt in
    monkeypatch.setattr(app_module, "CLIENT_IP_HEADER", "cf-connecting-ip")
    client.get("/r/lobby", headers=spoofed)
    assert ("203.0.113.9", "read") in app_module._buckets


def _dockerfile_cmd() -> list[str]:
    """The argv the shipped image actually runs, out of the CMD JSON array."""
    raw = (Path(__file__).resolve().parents[1] / "docker" / "Dockerfile").read_text()
    body = raw.split("\nCMD ", 1)[1].replace("\\\n", " ")
    return json.loads(body[: body.index("]") + 1])


def test_shipped_image_does_not_let_uvicorn_rewrite_the_client_address():
    """The test above proves the *app* trusts no forwarded header — but it runs through
    TestClient, which never touches uvicorn, so it stayed green while the image shipped
    `--proxy-headers --forwarded-allow-ips "*"`. That combination makes uvicorn overwrite
    scope["client"] from X-Forwarded-For for ANY peer, and client_ip() falls back to exactly
    that value: the rate limiter, the write budget and the long-poll caps all keyed on a
    number the caller chose. The guarantee lives below the app, so it is asserted below the
    app — against the argv the image runs and against uvicorn's own middleware.
    """
    cmd = _dockerfile_cmd()
    assert "--no-proxy-headers" in cmd
    assert "--proxy-headers" not in cmd
    # `--forwarded-allow-ips` is meaningless without proxy headers and is the flag that made
    # this dangerous; nothing should reintroduce it without revisiting the reasoning above.
    assert "--forwarded-allow-ips" not in cmd


def test_trusting_every_peer_would_hand_the_caller_its_own_rate_limit_identity():
    """What the flag above buys, demonstrated rather than asserted from memory: the same
    remote peer, the same header, the two trust settings. Pinning the failure mode means a
    future uvicorn that changes this behaviour is caught here rather than in production."""
    import asyncio
    from typing import Any, cast

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen: dict[str, tuple] = {}

    async def sink(scope, receive, send):
        seen["client"] = scope["client"]

    def client_seen_by_app(trusted: str) -> str:
        scope = {
            "type": "http",
            "client": ("203.0.113.9", 54321),  # an ordinary caller, not a proxy
            "scheme": "http",
            "headers": [(b"x-forwarded-for", b"1.2.3.4"), (b"host", b"x")],
        }
        # A hand-built scope and no receive/send: this probes the middleware's rewrite step,
        # which reads `client` and `headers` and forwards the rest untouched to `sink`. Cast
        # because the real signature wants full ASGI callables that nothing here calls.
        mw = cast(Any, ProxyHeadersMiddleware(sink, trusted_hosts=trusted))
        asyncio.run(mw(scope, None, None))
        return seen["client"][0]

    assert client_seen_by_app("*") == "1.2.3.4"  # what the image used to do
    assert client_seen_by_app("127.0.0.1") == "203.0.113.9"  # real peer survives


def test_idle_rooms_are_reaped_so_squatting_expires(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 2)
    store.append(tmp_path, "squat", "bot", "hi")
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(store.room_path(tmp_path, "squat"), (old, old))
    (tmp_path / ".reaped").unlink(missing_ok=True)  # force a reap pass
    store.append(tmp_path, "fresh", "bot", "hi")
    assert not store.room_path(tmp_path, "squat").exists()
    assert store.room_path(tmp_path, "fresh").exists()


def test_stillborn_rooms_go_after_a_day_but_answered_ones_keep_the_week(tmp_path):
    """One message nobody answered is worth a day; a conversation that stopped is worth a
    week. Both rooms are idle for the same time — only the reply tells them apart."""
    import store

    day_old = time.time() - store.STILLBORN_SECONDS - 60
    store.append(tmp_path, "monologue", "bot", "anyone here?")
    store.append(tmp_path, "answered", "bot", "anyone here?")
    store.append(tmp_path, "answered", "other", "yes")
    for room in ("monologue", "answered"):
        os.utime(store.room_path(tmp_path, room), (day_old, day_old))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert not store.room_path(tmp_path, "monologue").exists()
    assert store.room_path(tmp_path, "answered").exists()


def test_stillborn_room_survives_its_first_day(tmp_path):
    """The rule is 24h of silence, not "one message is disposable" — a room posted into an
    hour ago is exactly what a slow rendezvous looks like."""
    import store

    store.append(tmp_path, "waiting", "bot", "anyone here?")
    hour_old = time.time() - 3600
    os.utime(store.room_path(tmp_path, "waiting"), (hour_old, hour_old))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert store.room_path(tmp_path, "waiting").exists()


def test_stillborn_rule_does_not_touch_notes(tmp_path):
    """A note has no reply to wait for, so a single write says nothing about it. Notes keep
    the 7-day rule, and a topic must outlive the first day of the room it describes."""
    import store

    store.note_set(tmp_path, store.TOPIC_NS, "somewhere", "what this room is for")
    path = store.note_path(tmp_path, store.TOPIC_NS, "somewhere")
    day_old = time.time() - store.STILLBORN_SECONDS - 60
    os.utime(path, (day_old, day_old))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert path.exists()


def test_reap_spares_a_stillborn_room_answered_after_the_count(tmp_path, monkeypatch):
    """The under-lock recheck must re-count, not just re-stat: a reply landing mid-pass is
    exactly the message the reaper would otherwise delete."""
    import store

    store.append(tmp_path, "racing", "bot", "anyone here?")
    path = store.room_path(tmp_path, "racing")
    day_old = time.time() - store.STILLBORN_SECONDS - 60
    os.utime(path, (day_old, day_old))
    real_locked = store._locked

    @contextmanager
    def answer_then_lock(target):
        with real_locked(target):
            with target.open("ab") as f:  # a reply got in between the count and the unlink
                f.write(b'{"seq":2,"ts":"2026-01-01T00:00:00Z","from":"other","text":"yes"}\n')
            os.utime(target, (day_old, day_old))  # still idle: only the count saves it
            yield

    monkeypatch.setattr(store, "_locked", answer_then_lock)
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert path.exists()


def test_reverse_lines_reads_only_the_tail(tmp_path):
    import store

    p = tmp_path / "x.jsonl"
    p.write_bytes(b"".join(b'{"seq":%d}\n' % i for i in range(50_000)))
    with p.open("rb") as f:
        first = next(store.reverse_lines(f))
    assert first == b'{"seq":49999}'


def test_humans_page_is_static_and_never_interpolates_messages(client):
    # a message that would execute if the page ever built markup from user content
    payload = "<img src=x onerror=alert(1)>"
    client.post("/r/lobby", json={"from": "mallory", "text": payload})
    r = client.get("/humans")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert payload not in r.text and "mallory" not in r.text  # nothing user-supplied reaches HTML
    assert "innerHTML" not in r.text.replace("never innerHTML", "")  # textContent only


def test_humans_page_pins_its_inline_code_with_a_fresh_nonce(client):
    r1, r2 = client.get("/humans"), client.get("/humans")
    csp = r1.headers["content-security-policy"]
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in r1.text and f'<style nonce="{nonce}">' in r1.text
    assert "__NONCE__" not in r1.text
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r1.headers["content-security-policy"] != r2.headers["content-security-policy"]


def test_agent_surfaces_are_never_html(client):
    client.get("/r/lobby/say/bot/hi")
    for path in ("/", "/llms.txt", "/robots.txt", "/r/lobby", "/rooms", "/healthz"):
        r = client.get(path)
        assert r.headers["content-type"].startswith("text/plain"), path
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["cache-control"] == "no-store", path


def test_robots_keeps_rooms_out_of_indexes_but_invites_the_manual(client):
    body = client.get("/robots.txt").text
    assert "Disallow: /r/" in body and "Disallow: /kv/" in body
    assert "Allow: /" in body and "/llms.txt" in body
    assert client.get("/r/lobby").headers["x-robots-tag"] == "noindex"


def test_torn_final_line_costs_only_that_record(tmp_path):
    """The crash-recovery claim: a half-written last line must not poison the file."""
    import store

    for i in range(5):
        store.append(tmp_path, "crash", "bot", f"m{i}")
    path = store.room_path(tmp_path, "crash")
    with path.open("ab") as f:
        f.write(b'{"seq":6,"ts":"2026-01-01T00:00:00Z","from":"bot","te')  # power loss here
    view = store.read_messages(tmp_path, "crash", limit=50)
    assert [m["text"] for m in view["messages"]] == [f"m{i}" for i in range(5)]
    store.append(tmp_path, "crash", "bot", "after")  # and writing still works
    assert store.read_messages(tmp_path, "crash", limit=1)["messages"][0]["text"] == "after"


def test_concurrent_appends_never_duplicate_a_seq(tmp_path):
    import threading

    import store

    def hammer():
        for _ in range(40):
            store.append(tmp_path, "race", "bot", "x")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    view = store.read_messages(tmp_path, "race", limit=store.MAX_LIMIT)
    seqs = [m["seq"] for m in view["messages"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert store.last_seq(tmp_path, "race") == 160


def test_limit_is_clamped_to_the_response_budget(client):
    for _ in range(3):
        client.get("/r/lobby/say/bot/hi")
    assert client.get("/r/lobby?limit=999&format=json").json()["count"] == 3
    assert client.get("/r/lobby?limit=0&format=json").json()["count"] == 1  # floor of 1
    assert client.get("/r/lobby?limit=abc&format=json").json()["count"] == 3  # default


def test_cursor_past_the_end_returns_an_empty_but_usable_view(client):
    client.get("/r/lobby/say/bot/hi")
    view = client.get("/r/lobby?since=999&format=json").json()
    assert view["count"] == 0 and view["last_seq"] == 999  # cursor preserved, not reset to 0
    assert "(no new messages)" in client.get("/r/lobby?since=999").text


def test_oversize_and_empty_input_fail_closed(client):
    assert client.get("/r/lobby/say/bot/" + "x" * 4097).status_code == 400
    assert client.get("/r/lobby/say/bot/%20%20").status_code == 400  # whitespace-only
    assert client.post("/r/lobby", json={"from": "bot", "text": "x" * 4097}).status_code == 400
    assert client.get("/kv/ns/k/set/" + "y" * 8193).status_code == 400
    assert client.get("/rooms").text.strip().startswith("(no rooms")  # nothing was created


def test_a_full_length_message_is_accepted(client):
    """The cap is 4096, so 4096 must pass — an off-by-one here silently shrinks the limit."""
    assert client.post("/r/lobby", json={"from": "bot", "text": "x" * 4096}).status_code == 200
    assert client.get("/r/lobby?format=json").json()["messages"][0]["text"] == "x" * 4096


def test_unicode_survives_the_round_trip(client):
    client.post("/r/lobby", json={"from": "bot", "text": "héllo 世界 🌍"})
    assert "héllo 世界 🌍" in client.get("/r/lobby").text
    assert client.get("/r/lobby?format=json").json()["messages"][0]["text"] == "héllo 世界 🌍"


def test_notes_per_namespace_are_capped(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 2)
    store.note_set(tmp_path, "ns", "a", "1")
    store.note_set(tmp_path, "ns", "b", "2")
    store.note_set(tmp_path, "ns", "a", "overwrite is fine")  # existing key still writable
    with pytest.raises(store.StoreError, match="note limit"):
        store.note_set(tmp_path, "ns", "c", "3")
    store.note_set(tmp_path, "other", "c", "3")  # cap is per namespace, not global
    assert store.note_get(tmp_path, "ns", "a") == "overwrite is fine"


def test_reaper_spares_active_files_and_throttles_itself(tmp_path, monkeypatch):
    import store

    store.append(tmp_path, "active", "bot", "hi")
    store.note_set(tmp_path, "ns", "keep", "value")
    store.append(tmp_path, "other", "bot", "hi")
    assert store.room_path(tmp_path, "active").exists()
    assert store.note_get(tmp_path, "ns", "keep") == "value"

    # a reap ran on the first write, so the marker exists and the next pass is throttled
    marker = tmp_path / ".reaped"
    assert marker.exists()
    stale = time.time() - store.IDLE_SECONDS - 60
    os.utime(store.room_path(tmp_path, "other"), (stale, stale))
    store.append(tmp_path, "active", "bot", "again")
    assert store.room_path(tmp_path, "other").exists()  # throttled: not reaped yet
    marker.unlink()
    store.append(tmp_path, "active", "bot", "third")
    assert not store.room_path(tmp_path, "other").exists()  # now it is


def test_rooms_overview_carries_stats_newest_first(client, tmp_path):
    import store

    client.get("/r/old/say/bot/first")
    client.get("/r/busy/say/bot/a")
    client.get("/r/busy/say/bot/b")
    stale = time.time() - 3600
    os.utime(store.room_path(tmp_path, "old"), (stale, stale))

    view = client.get("/rooms?format=json").json()
    names = [r["room"] for r in view["rooms"]]
    assert "events" in names  # the server announced both rooms
    assert [n for n in names if n != "events"] == ["busy", "old"]  # recency, not alphabetical
    by_name = {r["room"]: r for r in view["rooms"]}
    assert by_name["busy"]["last_seq"] == 2 and by_name["busy"]["bytes"] > 0
    assert by_name["busy"]["idle_seconds"] < 60
    assert by_name["old"]["idle_seconds"] >= 3600
    assert view["total"] == 3 and view["capacity"] == store.MAX_ROOMS and view["bytes"] > 0

    body = client.get("/rooms").text
    assert "3 of 3 rooms" in body and "/r/busy" in body and "seq 2" in body and "ago" in body


def test_rooms_overview_hides_private_rooms_and_survives_an_empty_store(client):
    assert "no rooms yet" in client.get("/rooms").text
    assert client.get("/rooms?format=json").json() == {
        "rooms": [],
        "total": 0,
        "capacity": 512,
        "bytes": 0,
        "notes": {"total": 0, "bytes": 0, "capacity": 4096},
        "engagement": {
            "window_cap": 200,
            "windowed_messages": 0,
            "zero_response_share": None,
            "nick_diversity": None,
            "windowed_note_to_message_ratio": None,
        },
    }
    client.get("/r/p-secret/say/bot/hi")
    view = client.get("/rooms?format=json").json()
    assert view["total"] == 0 and view["rooms"] == []  # p- stays invisible in stats too


def test_rooms_overview_limits_the_tail_reads_it_does(client, tmp_path):
    import store

    for i in range(8):
        client.get(f"/r/room{i}/say/bot/hi")
    view = client.get("/rooms?limit=3&format=json").json()
    # 8 rooms + the events room the first of them created
    assert len(view["rooms"]) == 3 and view["total"] == 9  # count is complete, detail is capped
    assert store.room_stats(tmp_path, limit=0)["rooms"] != []  # limit floors at 1, never 0
    # junk limits fall back rather than 500 (the _cursor rule, incl. Unicode digits)
    for bad in ("abc", "\u00b2", "-4", ""):
        assert client.get(f"/rooms?limit={bad}&format=json").status_code == 200


# -------------------------------------------------- engagement tripwires (analysis §II.2.2)


def _stats_for(tmp_path, room):
    import store

    return {r["room"]: r for r in store.room_stats(tmp_path)["rooms"]}[room]


def test_engagement_flags_a_room_only_one_nick_ever_wrote_in(tmp_path):
    """The Moltbook 93.5% analog: nobody ever answered, so every message is unanswered."""
    import store

    for i in range(5):
        store.append(tmp_path, "monologue", "solo", f"m{i}")
    row = _stats_for(tmp_path, "monologue")
    assert row["window"] == 5
    assert row["zero_response_share"] == 1.0
    assert row["nick_diversity"] == 0.2  # 1 nick / 5 messages — the floor for this window


def test_engagement_counts_a_message_as_answered_only_if_a_different_nick_follows(tmp_path):
    import store

    for nick in ("a", "a", "b", "b", "b"):  # oldest first
        store.append(tmp_path, "talk", nick, "hi")
    row = _stats_for(tmp_path, "talk")
    # a's two messages are both followed by b; b's three are followed only by b
    assert row["zero_response_share"] == 0.6
    assert row["nick_diversity"] == 0.4  # 2 distinct nicks / 5 messages


def test_engagement_reports_no_data_rather_than_zero_for_an_empty_window(client, tmp_path):
    (tmp_path / "rooms").mkdir(parents=True)
    (tmp_path / "rooms" / "junk.jsonl").write_bytes(b"not a record\n")
    row = _stats_for(tmp_path, "junk")
    assert row == {
        "room": "junk",
        "last_seq": 0,
        "bytes": 13,
        "idle_seconds": 0,
        "topic": None,
        "window": 0,
        "zero_response_share": None,  # "no messages" is not "0% unanswered"
        "nick_diversity": None,
    }
    e = client.get("/rooms?format=json").json()["engagement"]
    assert e["windowed_messages"] == 0 and e["zero_response_share"] is None
    assert e["windowed_note_to_message_ratio"] is None  # no divide-by-zero, no fake 0.0
    assert "# engagement" not in client.get("/rooms").text  # nothing to report, so no line


def test_engagement_window_binds_before_the_ring_does(tmp_path, monkeypatch):
    """The metrics are over the scanned window, not over room history — so a room whose
    older half looks different must score on the window, and say how big it was."""
    import store

    for i in range(7):
        store.append(tmp_path, "shift", "alice", f"m{i}")
    for i in range(5):
        store.append(tmp_path, "shift", "bob", f"n{i}")
    monkeypatch.setattr(store, "WINDOW_MESSAGES", 5)
    row = _stats_for(tmp_path, "shift")
    assert row["window"] == 5 and row["last_seq"] == 12  # window < ring, cursor still exact
    assert row["zero_response_share"] == 1.0  # the newest 5 are all bob's
    assert row["nick_diversity"] == 0.2


def test_engagement_rollup_pools_every_scanned_window(client):
    client.get("/kv/plans/next/set/ship")
    for _ in range(3):
        client.get("/r/solo/say/s/hi")
    for nick in ("a", "b", "a", "b"):
        client.get(f"/r/chat/say/{nick}/hi")

    e = client.get("/rooms?format=json").json()["engagement"]
    # solo 3 unanswered + chat 1 + the 2 server lines in /r/events, over 9 messages
    assert e["window_cap"] == 200 and e["windowed_messages"] == 9
    assert e["zero_response_share"] == 0.6667
    assert e["nick_diversity"] == 0.4444  # {s, a, b, server} / 9 — pooled, not per room
    assert e["windowed_note_to_message_ratio"] == 0.1111  # 1 note, windowed denominator

    body = client.get("/rooms").text
    line = [ln for ln in body.splitlines() if ln.startswith("# engagement")]
    assert line == [
        "# engagement over 9 msgs scanned: zero-response 67%, nick diversity 0.44, notes/msg 0.11"
    ]


def test_rooms_metrics_never_scan_past_the_window_per_room(client, tmp_path, monkeypatch):
    """The cost bar: /rooms stays O(shown) x window, never a ring scan across 512 rooms."""
    import store

    for i in range(3):
        for j in range(30):
            store.append(tmp_path, f"room{i}", f"bot{j % 3}", "hi")
    monkeypatch.setattr(store, "WINDOW_MESSAGES", 10)
    real = store.reverse_lines
    passes = []

    def counted(f, chunk_size=65536, max_bytes=store.READ_BUDGET):
        seen = [max_bytes, 0]
        passes.append(seen)  # recorded up front: the caller abandons the generator early
        for line in real(f, chunk_size=chunk_size, max_bytes=max_bytes):
            seen[1] += 1
            yield line

    monkeypatch.setattr(store, "reverse_lines", counted)
    view = client.get("/rooms?limit=2&format=json").json()
    assert len(view["rooms"]) == 2 and view["total"] == 4  # 3 rooms + events, only 2 scanned
    assert len(passes) == 2, "one tail pass per SHOWN room, not per room on disk"
    for max_bytes, lines in passes:
        assert max_bytes == store.WINDOW_BYTES  # bounded in bytes...
        assert lines <= 10  # ...and in records: it stops at the window, not at EOF
    assert max(lines for _, lines in passes) == 10  # a 30-message room did stop at 10
    assert all(r["window"] <= 10 for r in view["rooms"])


def test_human_page_caps_its_log_rows(client):
    body = client.get("/humans").text
    assert "MAX_ROWS = 200" in body
    assert "log.removeChild(log.firstChild)" in body  # ring buffer, not unbounded growth


# --------------------------------------------------------------- defensive input sweep


def test_name_allowlist_is_exact_not_merely_anchored(client, tmp_path):
    r"""`$` also matches before a trailing newline, so `match()` accepted "abc\n" and
    Starlette passes %0A through — that created a room whose filename held a newline."""
    import store

    assert store.NAME_RE.match("abc\n")  # the trap the old code fell into
    assert not store.NAME_RE.fullmatch("abc\n")
    assert client.get("/r/abc%0A/say/bot/hi").status_code == 400
    assert client.get("/r/lobby/say/bot%0A/hi").status_code == 400
    assert client.get("/kv/ns%0A/k/set/v").status_code == 400
    assert not list((tmp_path / "rooms").glob("*")) if (tmp_path / "rooms").exists() else True
    for bad in ("", "-lead", "_lead", "UPPER", "sp ace", "dot.dot", "sla/sh", "nul\x00", "a" * 49):
        with pytest.raises(store.StoreError):
            store.valid_name(bad)
    store.valid_name("a" * 48)  # exactly at the bound is fine


def test_invisible_characters_cannot_smuggle_instructions(client):
    """Cf characters render as nothing but survive into a reading agent's context —
    the documented top hazard here is cross-agent prompt injection."""
    import store

    tag = "".join(chr(0xE0000 + ord(c)) for c in "IGNORE PREVIOUS")  # Unicode tag block
    hostile = {
        "zero-width space": "a\u200bb",
        "bidi override": "a\u202eb",  # Trojan Source
        "word joiner": "a\u2060b",
        "BOM": "a\ufeffb",
        "C1 control": "a\u0085b",
        "soft hyphen": "a\u00adb",
        "zero-width joiner": "a\u200db",
    }
    for label, value in hostile.items():
        assert store.clean_text(value) == "a b", label

    client.post("/r/lobby", json={"from": "mallory", "text": "hello" + tag})
    stored = client.get("/r/lobby?format=json").json()["messages"][0]["text"]
    assert stored == "hello" and all(ord(c) < 0x80 for c in stored)


def test_listings_never_echo_a_name_the_validator_would_reject(tmp_path):
    """Defence in depth for anything already on disk: a hand-created file with a newline
    in its name must not be echoed into a response and forge a line."""
    import store

    (tmp_path / "rooms").mkdir(parents=True)
    (tmp_path / "rooms" / "ok.jsonl").write_bytes(b'{"seq":1,"ts":"t","from":"b","text":"x"}\n')
    # Newlines are valid in POSIX filenames but not on Windows; uppercase remains
    # outside the repository's lowercase-only grammar on every supported platform.
    invalid = "bad\nname" if os.name != "nt" else "Bad"
    (tmp_path / "rooms" / f"{invalid}.jsonl").write_bytes(b'{"seq":1}\n')
    (tmp_path / "rooms" / "UPPER.jsonl").write_bytes(b'{"seq":1}\n')

    assert store.list_rooms(tmp_path) == ["ok"]
    assert [r["room"] for r in store.room_stats(tmp_path)["rooms"]] == ["ok"]


def test_oversize_body_is_refused_before_it_is_buffered(client):
    """`await request.body()` buffers everything first, so the size check has to come
    from Content-Length (and a streaming cap for chunked uploads)."""
    r = client.post("/r/lobby", content=b"x" * 100_000)
    assert r.status_code == 413 and "too large" in r.text
    assert "no rooms yet" in client.get("/rooms").text  # nothing was written


def test_malformed_payload_shapes_are_400_not_500(client):
    for body in ("[1,2,3]", '"a string"', "42", "null", "true"):
        r = client.post(
            "/r/lobby", content=body.encode(), headers={"content-type": "application/json"}
        )
        assert r.status_code == 400, body
    assert client.post("/r/lobby", content=b"{not json").status_code == 400
    assert client.post("/r/lobby", json={}).status_code == 400  # empty from/text


def test_numeric_inputs_cannot_overflow_or_amplify(client, tmp_path):
    import app as app_module
    import store

    client.get("/r/lobby/say/bot/hi")
    # Python refuses int() past 4300 digits; _cursor must fall back, not propagate.
    # (A 100k-char URL never reaches the app — the client and uvicorn reject the request
    # line first — so the parser is exercised directly.)
    assert app_module._cursor("9" * 100_000, 50) == 50
    assert app_module._cursor("9" * 4000, 50) == int("9" * 4000)
    assert client.get("/r/lobby?since=" + "9" * 5000).status_code == 200
    assert client.get("/r/lobby?limit=" + "9" * 5000).status_code == 200
    assert client.get(f"/r/lobby?since={2**70}&format=json").json()["count"] == 0
    # /rooms detail is clamped, so one cheap request cannot force unbounded tail reads
    assert len(store.room_stats(tmp_path, limit=10**9)["rooms"]) <= store.MAX_LIMIT
    assert client.get("/rooms?limit=999999999&format=json").status_code == 200


# ------------------------------------------------------------- right-sized wire limits


def test_header_block_is_capped_far_below_the_edge_ceiling(client):
    """A real block through Cloudflare is ~13 headers / ~400 bytes. The cap is ~10x that,
    and 32x tighter than Cloudflare's own 128 KiB."""
    import app as app_module

    assert client.get("/healthz", headers={"x-pad": "v" * 100}).status_code == 200

    many = {f"x-pad-{i}": "v" for i in range(app_module.MAX_HEADERS + 5)}
    r = client.get("/healthz", headers=many)
    assert r.status_code == 431 and "header block too large" in r.text

    big = {"x-big": "v" * (app_module.MAX_HEADER_BYTES + 100)}
    r = client.get("/healthz", headers=big)
    assert r.status_code == 431
    assert "a plain GET with no custom headers" in r.text  # tells the client what to do


def test_a_full_length_message_is_postable_in_every_encoding(client):
    """The documented cap is 2000 *characters*. json.dumps defaults to ensure_ascii=True,
    so 2000 emoji become ~24 KB of \\uXXXX — the old 8 KiB body cap rejected legal
    messages, silently shrinking the documented limit for non-Latin scripts."""
    import json

    for label, ch in (("ascii", "a"), ("cjk", "日"), ("emoji", "\U0001f600")):
        text_value = ch * 2000
        escaped = json.dumps({"from": "agent", "text": text_value}, ensure_ascii=True)
        r = client.post(
            f"/r/enc-{label}",
            content=escaped.encode(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 200, (label, len(escaped), r.text[:200])
        stored = client.get(f"/r/enc-{label}?format=json").json()["messages"][0]["text"]
        assert len(stored) == 2000 and stored == text_value


def test_body_cap_still_refuses_oversize_uploads(client):
    import app as app_module

    over = b"x" * (app_module.MAX_BODY + 1000)
    assert client.post("/r/lobby", content=over).status_code == 413
    # declared-but-not-sent is refused on Content-Length alone, before buffering
    assert client.post("/r/lobby", content=b"x" * 100, headers={"content-length": "99999999"})


def test_notes_have_a_post_lane_so_their_documented_cap_is_reachable(client):
    """8192 characters URL-encode past the request line (and past Cloudflare's 16 KiB URL
    ceiling), so without POST the note limit was unreachable."""
    import store

    value = "z" * store.MAX_VALUE_CHARS  # not "v": the banner contains one
    r = client.post("/kv/plans/big", json={"value": value})
    assert r.status_code == 200
    assert client.get("/kv/plans/big").text.count("z") == store.MAX_VALUE_CHARS
    assert (
        client.post(
            "/kv/plans/toobig", json={"value": "z" * (store.MAX_VALUE_CHARS + 1)}
        ).status_code
        == 400
    )


# ------------------------------------------------------- conditional notes (CAS)


def test_cas_rejects_a_write_whose_read_went_stale(client):
    client.get("/kv/coord/leader/set/none")
    # Both agents read "none"; both try to claim. Exactly one may win.
    first = client.get("/kv/coord/leader/set/agent-a?if=none")
    second = client.get("/kv/coord/leader/set/agent-b?if=none")
    assert first.status_code == 200
    assert second.status_code == 409
    assert "agent-a" in client.get("/kv/coord/leader").text  # loser did not clobber
    assert "agent-a" in second.text  # 409 hands back the current value to rebase on


def test_if_absent_creates_exactly_once(client):
    assert client.get("/kv/coord/claim/set/agent-a?if_absent=1").status_code == 200
    assert client.get("/kv/coord/claim/set/agent-b?if_absent=1").status_code == 409
    assert "agent-a" in client.get("/kv/coord/claim").text


def test_cas_distinguishes_absent_from_empty_and_works_over_post(client):
    # An empty string is a legal value, so absence cannot be encoded as if=<empty>.
    assert client.post("/kv/coord/n", json={"value": "0", "if_absent": True}).status_code == 200
    r = client.post("/kv/coord/n", json={"value": "1", "if": "0"})
    assert r.status_code == 200
    assert client.post("/kv/coord/n", json={"value": "2", "if": "0"}).status_code == 409
    assert "1" in client.get("/kv/coord/n").text


def test_unconditional_write_still_overwrites(client):
    client.get("/kv/coord/plain/set/one")
    assert client.get("/kv/coord/plain/set/two").status_code == 200  # no condition, no conflict
    assert "two" in client.get("/kv/coord/plain").text


# ------------------------------------------------------------------ long polling


def test_wait_returns_immediately_when_messages_already_exist(client):
    client.get("/r/lobby/say/bot/first")
    started = time.monotonic()
    r = client.get("/r/lobby?since=0&wait=10")
    assert r.status_code == 200 and "first" in r.text
    assert time.monotonic() - started < 2  # did not park on a room that already had data


def test_wait_is_capped_and_returns_empty_rather_than_hanging(client):
    client.get("/r/lobby/say/bot/only")
    started = time.monotonic()
    r = client.get("/r/lobby?since=1&wait=99")  # asks for 99s, ceiling is MAX_WAIT
    elapsed = time.monotonic() - started
    assert r.status_code == 200 and "no new messages" in r.text
    assert elapsed < 30, f"wait was not clamped to MAX_WAIT: {elapsed}s"


def test_wait_without_since_does_not_park(client):
    client.get("/r/lobby/say/bot/hi")
    started = time.monotonic()
    client.get("/r/lobby?wait=10")  # no cursor: nothing to wait for
    assert time.monotonic() - started < 2


def test_waiter_slots_are_bounded_per_ip(client):
    import app

    with app._waiter_slot("1.2.3.4") as a, app._waiter_slot("1.2.3.4") as b:
        assert a and b
        with app._waiter_slot("1.2.3.4") as c, app._waiter_slot("1.2.3.4") as d:
            assert c and d
            with app._waiter_slot("1.2.3.4") as e:
                assert e is False  # 5th concurrent waiter from one IP is refused
            with app._waiter_slot("5.6.7.8") as other:
                assert other is True  # a different IP is unaffected
    assert app._waiters_total == 0  # every slot released
    assert app._waiters_by_ip == {}  # and the table does not grow per distinct IP


# ------------------------------------------------------------- record format / stats


def test_timestamps_carry_microseconds_and_seq_stays_authoritative(client):
    for _ in range(3):
        client.get("/r/lobby/say/bot/burst")
    msgs = client.get("/r/lobby?format=json").json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2, 3]  # contiguous total order
    for m in msgs:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", m["ts"]), m["ts"]


def test_old_second_precision_records_still_parse(tmp_path, monkeypatch):
    """Records written before microsecond timestamps must keep reading — `ts` is opaque."""
    import store

    room = store.room_path(tmp_path, "legacy")
    room.parent.mkdir(parents=True, exist_ok=True)
    room.write_text('{"seq":1,"ts":"2026-01-01T00:00:00Z","from":"old","text":"hi"}\n')
    view = store.read_messages(tmp_path, "legacy")
    assert view["messages"][0]["text"] == "hi" and view["last_seq"] == 1
    store.append(tmp_path, "legacy", "new", "next")  # and appending after them works
    assert store.read_messages(tmp_path, "legacy")["last_seq"] == 2


def test_rooms_reports_note_usage_without_naming_namespaces(client):
    client.get("/kv/p-secretns/k/set/hello")
    body = client.get("/rooms").text
    assert "notes 1 of 4096" in body
    assert "p-secretns" not in body  # aggregate only: namespaces stay unenumerable
    stats = client.get("/rooms?format=json").json()["notes"]
    assert stats["total"] == 1 and stats["bytes"] == 5 and stats["capacity"] == 4096


def test_newlines_are_flattened_in_both_write_lanes(client):
    """llms.txt used to promise POST carried multi-line text. It never did."""
    client.post("/r/lobby", json={"from": "bot", "text": "line1\nline2\r\nline3"})
    # one space per stripped character, so CRLF leaves two — nothing is silently merged
    assert "line1 line2  line3" in client.get("/r/lobby").text
    assert client.get("/r/lobby/say/bot/a%0Ab").status_code == 404  # not routable in a path
    manual = client.get("/llms.txt").text
    assert "no multi-line message" in manual


# ------------------------------------------------------------- room discovery (events)


def test_new_public_rooms_are_announced(client):
    client.get("/r/alpha/say/bot/hi")
    client.get("/r/beta/say/bot/hi")
    body = client.get("/r/events").text
    assert "created alpha" in body and "created beta" in body
    assert "<~server>" in body  # server-authored — and unsigned, like every nick


def test_a_room_is_announced_once_not_per_message(client):
    for _ in range(3):
        client.get("/r/gamma/say/bot/hi")
    assert client.get("/r/events").text.count("created gamma") == 1


def test_private_rooms_are_never_announced(client):
    client.get("/r/public1/say/bot/hi")  # brings the events room into existence
    client.get("/r/p-7f3a9c/say/bot/secret")
    body = client.get("/r/events").text
    assert "p-7f3a9c" not in body  # not the name...
    assert body.count("created") == 1  # ...and not an anonymous line either (timing leak)


def test_events_room_does_not_announce_itself(client):
    client.get("/r/alpha/say/bot/hi")
    assert "created events" not in client.get("/r/events").text


def test_clients_cannot_forge_events(client):
    """A discovery log a stranger can append to is worse than no log."""
    assert client.get("/r/events/say/attacker/created%20evil-room").status_code == 403
    assert client.post("/r/events", json={"from": "x", "text": "created evil"}).status_code == 403
    client.get("/r/real/say/bot/hi")
    body = client.get("/r/events").text
    assert "evil" not in body and "created real" in body


def test_events_is_readable_with_since_and_json_like_any_room(client):
    client.get("/r/one/say/bot/hi")
    client.get("/r/two/say/bot/hi")
    view = client.get("/r/events?since=1&format=json").json()
    assert [m["text"] for m in view["messages"]] == ["created two"]


def test_a_failed_announcement_never_fails_the_write(tmp_path, monkeypatch):
    """The caller's message is already fsynced when the event is written."""
    import store

    monkeypatch.setattr(store, "MAX_ROOMS", 1)
    rec = store.append(tmp_path, "solo", "bot", "hi")  # events cannot fit under the cap
    assert rec["seq"] == 1  # the user's write still succeeded
    assert not store.room_path(tmp_path, "events").exists()


# ------------------------------------------------------------ signed writes (did:key)


def _keypair(seed: int = 1):
    """A deterministic Ed25519 key and its did:key, so a failure is reproducible."""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    import didkey

    key = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    raw = key.public_key().public_bytes_raw()

    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(didkey.MULTICODEC_ED25519 + raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out

    def sign(message: str) -> str:
        return base64.urlsafe_b64encode(key.sign(message.encode())).decode().rstrip("=")

    return f"{didkey.PREFIX}z{out}", sign


def _say_signed(client, room, did, sign, text, nonce=1):
    """The canonical string is `room|nonce|text` over the *swept* text — what is stored."""
    import store

    body = store.clean_text(text)
    return client.get(f"/r/{room}/say-signed/{did}/{sign(f'{room}|{nonce}|{body}')}/{nonce}/{text}")


def test_a_signed_write_is_attributed_to_the_key_not_a_nickname(client):
    did, sign = _keypair()
    r = _say_signed(client, "lobby", did, sign, "signed hello")
    assert r.status_code == 200
    view = client.get("/r/lobby?format=json").json()
    assert view["messages"][0]["from"] == did  # json carries the DID in full
    assert view["messages"][0]["nonce"] == 1
    # the text view abbreviates: 56 base58 characters per line would be the whole budget
    body = client.get("/r/lobby").text
    assert f"<{did[len('did:key:') :][:4]}…{did[-4:]}> signed hello" in body
    assert did not in body


def test_signed_writes_fail_closed_on_every_malformed_credential(client):
    did, sign = _keypair()
    other, _ = _keypair(seed=2)
    good = sign("lobby|1|hi")
    assert client.get(f"/r/lobby/say-signed/{did}/{good}/1/hi").status_code == 200
    # a valid signature from a different key
    assert client.get(f"/r/lobby/say-signed/{other}/{good}/2/hi").status_code == 403
    # a signature over different text
    assert client.get(f"/r/lobby/say-signed/{did}/{sign('lobby|2|other')}/2/hi").status_code == 403
    # a signature over a different room: room is inside the signed string for this reason
    assert client.get(f"/r/other/say-signed/{did}/{sign('lobby|2|hi')}/2/hi").status_code == 403
    # malformed dids and signatures never reach the verifier
    for bad_did in ("did:key:zNotAKey", "did:web:example.com", "z6Mk", "did:key:" + "z" * 48):
        assert client.get(f"/r/lobby/say-signed/{bad_did}/{good}/9/hi").status_code == 400, bad_did
    for bad_sig in ("x", good[:-1], good + "AA", good.replace("_", "+")):
        assert client.get(f"/r/lobby/say-signed/{did}/{bad_sig}/9/hi").status_code in (400, 403)
    for bad_nonce in ("abc", "-1", "1.5", "9" * 20):
        assert client.get(f"/r/lobby/say-signed/{did}/{good}/{bad_nonce}/hi").status_code == 400
    assert client.get("/r/lobby?format=json").json()["count"] == 1  # only the good one landed


def test_a_replayed_signed_url_is_refused_while_the_message_is_still_there(client):
    did, sign = _keypair()
    url = f"/r/lobby/say-signed/{did}/{sign('lobby|7|once')}/7/once"
    assert client.get(url).status_code == 200
    r = client.get(url)  # the identical captured URL, again
    assert r.status_code == 400 and "not greater than 7" in r.text
    assert (
        client.get(f"/r/lobby/say-signed/{did}/{sign('lobby|6|older')}/6/older").status_code == 400
    )
    assert client.get(f"/r/lobby/say-signed/{did}/{sign('lobby|8|next')}/8/next").status_code == 200
    assert client.get("/r/lobby?format=json").json()["count"] == 2


def test_the_signature_covers_the_swept_text_not_the_raw_text(client):
    """Both directions, so the contract is unambiguous: what is stored is what was signed.

    A record whose signature covered pre-sweep bytes could never be re-verified from the
    room, because the pre-sweep bytes are exactly what the store refuses to keep.
    """
    import store

    did, sign = _keypair()
    raw = "hi\u200bthere"  # a zero-width space the sweep turns into a plain space
    swept = store.clean_text(raw)
    assert swept == "hi there"
    signed_raw = sign(f"lobby|1|{raw}")
    assert client.get(f"/r/lobby/say-signed/{did}/{signed_raw}/1/{raw}").status_code == 403
    signed_swept = sign(f"lobby|1|{swept}")
    assert client.get(f"/r/lobby/say-signed/{did}/{signed_swept}/1/{raw}").status_code == 200
    assert client.get("/r/lobby?format=json").json()["messages"][0]["text"] == swept


def test_the_signed_lane_also_works_over_post(client):
    did, sign = _keypair()
    r = client.post(
        "/r/lobby",
        json={"did": did, "sig": sign("lobby|3|via post"), "nonce": "3", "text": "via post"},
    )
    assert r.status_code == 200
    assert client.get("/r/lobby?format=json").json()["messages"][0]["from"] == did
    bad = client.post(
        "/r/lobby", json={"did": did, "sig": sign("lobby|4|x"), "nonce": "4", "text": "y"}
    )
    assert bad.status_code == 403


def test_signed_writes_pay_the_write_budget_like_any_other(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "RATE_WRITE", 2)
    did, sign = _keypair()
    codes = [
        _say_signed(client, "lobby", did, sign, f"m{i}", nonce=i).status_code for i in (1, 2, 3)
    ]
    assert codes == [200, 200, 429]


def test_an_unsigned_nick_can_never_look_verified(client):
    """`from` is the provenance field, so the unsigned lane must not be able to reach the
    DID shape — the name allowlist rejects ':' and that is what keeps the lanes apart."""
    import store

    assert client.get("/r/lobby/say/did:key:z6Mkfake/hi").status_code in (400, 404)
    with pytest.raises(store.StoreError):
        store.valid_name("did:key:z6Mkfake")


# ----------------------------------------------------------------------- room topics


def test_a_topic_is_a_reserved_note_rendered_beside_the_room(client):
    client.get("/r/lobby/say/bot/hi")
    assert client.get("/kv/topic/lobby/set/where%20agents%20meet").status_code == 200
    body = client.get("/rooms").text
    assert "/r/lobby" in body and "where agents meet" in body
    by_name = {r["room"]: r for r in client.get("/rooms?format=json").json()["rooms"]}
    assert by_name["lobby"]["topic"] == "where agents meet"
    assert by_name["events"]["topic"] is None  # no topic note, no invention


def test_a_topic_passes_the_same_sweep_and_cas_as_any_note(client):
    import store

    client.get("/r/lobby/say/bot/hi")
    tag = "".join(chr(0xE0000 + ord(c)) for c in "IGNORE")  # invisible instruction smuggling
    client.post("/kv/topic/lobby", json={"value": "plans" + tag})
    shown = {r["room"]: r for r in client.get("/rooms?format=json").json()["rooms"]}
    assert shown["lobby"]["topic"] == "plans"
    # a topic is set with the ordinary note lane, so `if=` settles a clobber race
    assert client.get("/kv/topic/lobby/set/mine?if=plans").status_code == 200
    assert client.get("/kv/topic/lobby/set/yours?if=plans").status_code == 409
    # long topics are previewed in the overview; the note still holds the whole thing
    client.post("/kv/topic/lobby", json={"value": "z" * 400})
    rooms = {r["room"]: r for r in client.get("/rooms?format=json").json()["rooms"]}
    preview = rooms["lobby"]["topic"]
    assert len(preview) == store.TOPIC_PREVIEW_CHARS + 1 and preview.endswith("…")
    assert client.get("/kv/topic/lobby").text.count("z") == 400


def test_the_human_page_renders_topics_as_text_never_markup(client):
    body = client.get("/humans").text
    assert "topic" in body and "innerHTML" not in body.replace("never innerHTML", "")


# --------------------------------------------------------------------------- mailbox


def test_a_mailbox_room_refuses_the_unsigned_lane(client):
    r = client.get("/r/mb-inbox/say/spammer/free%20crypto")
    assert r.status_code == 403 and "signed writes only" in r.text
    assert "say-signed" in r.text  # the refusal tells an agent what to send instead
    assert client.post("/r/mb-inbox", json={"from": "spammer", "text": "hi"}).status_code == 403
    did, sign = _keypair()
    assert _say_signed(client, "mb-inbox", did, sign, "a real letter").status_code == 200
    # reads stay open: a mailbox is an append room, not a per-recipient inbox
    body = client.get("/r/mb-inbox").text
    assert "a real letter" in body
    # and the footer names the lane that works here, not the one that would 403
    assert "say:  /r/mb-inbox/say-signed/" in body
    assert "say:  /r/lobby/say/<nick>" in client.get("/r/lobby").text


def test_room_classes_compose_by_prefix(client):
    import store

    assert store.room_classes("mb-p-7f3a9c") == frozenset({"mb", "p"})
    assert store.room_classes("e-p-7f3a9c") == frozenset({"e", "p"})
    assert store.room_classes("lobby") == frozenset()
    assert store.room_classes("p-") == frozenset({"p"})  # the body is never a class
    assert store.room_classes("d") == frozenset()
    # a private mailbox is both: signed writes only, and never enumerated
    did, sign = _keypair()
    assert client.get("/r/mb-p-7f3a9c/say/bot/hi").status_code == 403
    assert _say_signed(client, "mb-p-7f3a9c", did, sign, "letter").status_code == 200
    assert "mb-p-7f3a9c" not in client.get("/rooms").text
    assert "mb-p-7f3a9c" not in client.get("/r/events").text  # nor announced
    assert "letter" in client.get("/r/mb-p-7f3a9c").text  # but reachable by name


# ---------------------------------------------------------------------- owned rooms


def _claim(client, room, did, sign, nonce=1):
    """A claim is a signed write storing the signer's own key. The nonce counter is per
    room and shared with room-allow, so claiming burns 1 and allow-list writes start at 2."""
    return _set_signed(client, "room-owners", room, did, sign, did, nonce)


def _set_signed(client, ns, key, did, sign, value, nonce=1):
    return client.get(
        f"/kv/{ns}/{key}/set-signed/{did}/{sign(f'{ns}|{key}|{nonce}|{value}')}/{nonce}/{value}"
    )


def test_only_d_rooms_are_ownable_and_the_front_door_never_is(client):
    did, sign = _keypair()
    assert _claim(client, "d-bounty", did, sign).status_code == 200
    for room in ("lobby", "meta", "open-room", "mb-inbox", "events"):
        r = _claim(client, room, did, sign)
        assert r.status_code == 403, room
        assert "Only d- rooms are ownable" in r.text
    # an established open room stays open: nobody can lock its writers out
    assert client.get("/r/lobby/say/bot/still%20open").status_code == 200


def test_a_claim_must_be_signed_by_the_key_it_stores(client):
    """The old check only asked whether `value` *parsed* as a did:key, so anyone could lock
    an unclaimed d- room to any key — including a stranger's, handing them a room they never
    asked for and locking everyone else out until the note idled away."""
    victim, _ = _keypair()
    attacker, attacker_sign = _keypair(seed=2)

    unsigned = client.get(f"/kv/room-owners/d-bounty/set/{victim}?if_absent=1")
    assert unsigned.status_code == 403 and "only its holder can sign with it" in unsigned.text

    # signing with a key you do hold, to store one you do not, is the same attack
    forged = _set_signed(client, "room-owners", "d-bounty", attacker, attacker_sign, victim)
    assert forged.status_code == 403
    assert client.get("/kv/room-owners/d-bounty").status_code == 404  # nothing was stored

    # and the room stays writable by everyone, because it was never actually claimed
    assert client.get("/r/d-bounty/say/anyone/still%20open").status_code == 200


def test_a_room_with_messages_can_no_longer_be_claimed(client):
    """Ownable-from-birth was documented in the un-ownable rooms' error text and never
    enforced for d- rooms, so a claim could be dropped on a conversation already running."""
    did, sign = _keypair()
    assert client.get("/r/d-busy/say/alice/hello").status_code == 200
    r = _claim(client, "d-busy", did, sign)
    assert r.status_code == 403 and "already has messages" in r.text
    assert client.get("/r/d-busy/say/bob/still%20here").status_code == 200


def test_ownership_guards_do_not_expire_out_from_under_a_live_room(tmp_path):
    """room-owners, room-allow and room-nonce were reaped on their own mtime, so 7 quiet
    days of *ownership* opened a still-busy room to a fresh claim, silently dropped the
    allow-list, and reset the counter that stops a captured URL re-adding a revoked key."""
    import store

    did = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    store.append(tmp_path, "d-live", "bot", "hi")
    for ns, value in ((store.OWNERS_NS, did), (store.ALLOW_NS, did), (store.NONCE_NS, "7")):
        store.note_set(tmp_path, ns, "d-live", value)
        old = time.time() - store.IDLE_SECONDS - 60
        os.utime(store.note_path(tmp_path, ns, "d-live"), (old, old))

    (tmp_path / ".reaped").unlink(missing_ok=True)
    store.append(tmp_path, "d-live", "bot", "still talking")  # forces a reap pass
    for ns in (store.OWNERS_NS, store.ALLOW_NS, store.NONCE_NS):
        assert store.note_get(tmp_path, ns, "d-live") is not None, ns

    # once the room itself goes, the guards go with it — bounded exactly as before
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(store.room_path(tmp_path, "d-live"), (old, old))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store.append(tmp_path, "elsewhere", "bot", "hi")
    assert not store.room_path(tmp_path, "d-live").exists()
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store.append(tmp_path, "elsewhere", "bot", "again")
    for ns in (store.OWNERS_NS, store.ALLOW_NS, store.NONCE_NS):
        assert store.note_get(tmp_path, ns, "d-live") is None, ns


def test_a_nickname_cannot_own_a_room(client):
    r = client.get("/kv/room-owners/d-bounty/set/alice?if_absent=1")
    assert r.status_code == 400 and "did:key" in r.text
    assert client.get("/kv/room-owners/d-bounty").status_code == 404  # nothing was written
    assert client.get("/r/d-bounty/say/alice/hi").status_code == 200  # unclaimed, still open


def test_an_owned_room_takes_writes_only_from_listed_keys(client):
    owner, owner_sign = _keypair()
    friend, friend_sign = _keypair(seed=2)
    stranger, stranger_sign = _keypair(seed=3)
    assert _claim(client, "d-bounty", owner, owner_sign).status_code == 200

    assert client.get("/r/d-bounty/say/anyone/hi").status_code == 403  # unsigned: refused
    assert _say_signed(client, "d-bounty", owner, owner_sign, "open for claims").status_code == 200
    assert _say_signed(client, "d-bounty", stranger, stranger_sign, "spam").status_code == 403

    # the allow-list is owner-only, and it is a signed note write
    assert (
        _set_signed(
            client, "room-allow", "d-bounty", friend, friend_sign, friend, nonce=2
        ).status_code
        == 403
    )
    assert (
        _set_signed(
            client, "room-allow", "d-bounty", owner, owner_sign, friend, nonce=2
        ).status_code
        == 200
    )
    assert _say_signed(client, "d-bounty", friend, friend_sign, "my claim").status_code == 200
    assert _say_signed(client, "d-bounty", stranger, stranger_sign, "still no").status_code == 403
    assert [m["from"] for m in client.get("/r/d-bounty?format=json").json()["messages"]] == [
        owner,
        friend,
    ]


def test_ownership_cannot_be_taken_by_overwriting_the_note(client):
    owner, owner_sign = _keypair()
    thief, thief_sign = _keypair(seed=2)
    _claim(client, "d-bounty", owner, owner_sign)
    # unconditional overwrite, CAS claim and a signed claim by a stranger: all refused
    assert client.get(f"/kv/room-owners/d-bounty/set/{thief}").status_code == 403
    assert _claim(client, "d-bounty", thief, thief_sign).status_code == 403
    assert (
        _set_signed(
            client, "room-owners", "d-bounty", thief, thief_sign, thief, nonce=2
        ).status_code
        == 403
    )
    assert client.get("/kv/room-owners/d-bounty").text.strip().endswith(owner)
    # the owner may hand it over, with its own signature
    assert (
        _set_signed(
            client, "room-owners", "d-bounty", owner, owner_sign, thief, nonce=2
        ).status_code
        == 200
    )
    assert _say_signed(client, "d-bounty", thief, thief_sign, "mine now").status_code == 200


def test_an_allow_list_needs_an_owner_and_fails_closed_on_junk(client):
    owner, owner_sign = _keypair()
    r = _set_signed(client, "room-allow", "d-orphan", owner, owner_sign, owner)
    assert r.status_code == 403 and "has no owner" in r.text
    _claim(client, "d-orphan", owner, owner_sign)
    bad = _set_signed(client, "room-allow", "d-orphan", owner, owner_sign, "alice", nonce=2)
    assert bad.status_code == 400 and "did:keys" in bad.text
    assert client.get("/kv/room-allow/d-orphan").status_code == 404


def test_signed_note_writes_are_scoped_to_the_two_ownership_namespaces(client):
    did, sign = _keypair()
    r = _set_signed(client, "plans", "next", did, sign, "ship")
    assert r.status_code == 400 and "world-writable" in r.text
    signed = {"value": "ship", "did": did, "sig": sign("plans|next|1|ship"), "nonce": "1"}
    assert client.post("/kv/plans/next", json=signed).status_code == 400
    assert (
        client.get("/kv/plans/next/set/ship").status_code == 200
    )  # the ordinary lane is untouched


def test_a_replayed_ownership_url_cannot_roll_an_allow_list_back(client):
    owner, owner_sign = _keypair()
    friend, _ = _keypair(seed=2)
    _claim(client, "d-bounty", owner, owner_sign)  # burns nonce 1
    url = f"/kv/room-allow/d-bounty/set-signed/{owner}/{owner_sign(f'room-allow|d-bounty|2|{friend}')}/2/{friend}"
    assert client.get(url).status_code == 200
    _set_signed(client, "room-allow", "d-bounty", owner, owner_sign, owner, nonce=3)  # revoke
    r = client.get(url)  # the captured URL that would re-add the revoked key
    assert r.status_code == 403 and "single-use" in r.text
    assert friend not in client.get("/kv/room-allow/d-bounty").text
    # the counter is server-written and world-readable, never client-writable
    assert client.get("/kv/room-nonce/d-bounty").text.strip().endswith("3")
    assert client.get("/kv/room-nonce/d-bounty/set/0").status_code == 403


# ------------------------------------------------------------------ ephemeral rooms


def _at(monkeypatch, store, stamp):
    monkeypatch.setattr(store, "_now", lambda: stamp)


def test_an_ephemeral_room_stops_returning_old_messages(client, tmp_path, monkeypatch):
    import store

    real_now = store._now
    _at(monkeypatch, store, "2020-01-01T00:00:00.000000Z")
    client.get("/r/e-deal/say/bot/stale%20offer")
    monkeypatch.setattr(store, "_now", real_now)
    client.get("/r/e-deal/say/bot/live%20offer")

    view = client.get("/r/e-deal?format=json").json()
    assert [m["text"] for m in view["messages"]] == ["live offer"]
    assert store.last_seq(tmp_path, "e-deal") == 2  # seq counts past what nobody can read
    assert "stale offer" not in client.get("/r/e-deal").text
    # ephemeral is not secret: the room is listed and announced like any other
    assert "e-deal" in client.get("/rooms").text
    assert "created e-deal" in client.get("/r/events").text


def test_ephemeral_expiry_is_lazy_but_rotation_reclaims_the_disk(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 4096)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 2048)
    real_now = store._now
    _at(monkeypatch, store, "2020-01-01T00:00:00.000000Z")
    for _ in range(40):
        store.append(tmp_path, "e-chat", "bot", "x" * 100)
    monkeypatch.setattr(store, "_now", real_now)
    for _ in range(30):
        store.append(tmp_path, "e-chat", "bot", "fresh")
    view = store.read_messages(tmp_path, "e-chat", limit=200)
    assert {m["text"] for m in view["messages"]} == {"fresh"}
    assert view["last_seq"] == 70 and view["first_seq"] > 40  # seq never rewinds; gap visible
    disk = store.room_path(tmp_path, "e-chat").read_text()
    assert "2020-01-01" not in disk  # rotation reclaimed the expired records
    assert store.room_path(tmp_path, "e-chat").stat().st_size <= 4096


def test_an_unparseable_timestamp_counts_as_expired(tmp_path):
    """Fail closed: a record whose age cannot be established cannot honour the promise."""
    import store

    room = store.room_path(tmp_path, "e-x")
    room.parent.mkdir(parents=True, exist_ok=True)
    room.write_text('{"seq":1,"ts":"whenever","from":"bot","text":"hi"}\n')
    assert store.read_messages(tmp_path, "e-x")["count"] == 0
    assert store.read_messages(tmp_path, "keeps-it")["count"] == 0  # a different room, empty


def test_ephemeral_and_private_compose(client, monkeypatch):
    import store

    real_now = store._now
    _at(monkeypatch, store, "2020-01-01T00:00:00.000000Z")
    client.get("/r/e-p-7f3a9c/say/bot/stale")
    monkeypatch.setattr(store, "_now", real_now)
    client.get("/r/e-p-7f3a9c/say/bot/live")
    assert store.room_classes("e-p-7f3a9c") == frozenset({"e", "p"})
    assert "live" in client.get("/r/e-p-7f3a9c").text
    assert "stale" not in client.get("/r/e-p-7f3a9c").text
    assert "e-p-7f3a9c" not in client.get("/rooms").text  # unlisted, and never announced
    assert "e-p-7f3a9c" not in client.get("/r/events").text


# ------------------------------------------------------------------ /skill.md alias


def test_skill_md_is_the_same_manual_and_is_never_rate_limited(client, monkeypatch):
    import app as app_module

    # Same bytes as the installable SKILL.md — one artifact, so the skill an agent
    # installs and the skill it fetches can never drift.
    skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

    assert client.get("/skill.md").text == skill
    assert client.get("/skill.md").headers["content-type"].startswith("text/plain")
    assert "/llms.txt" in client.get("/skill.md").text  # points at the full reference
    monkeypatch.setattr(app_module, "RATE_READ", 1)
    for _ in range(5):
        assert client.get("/skill.md").status_code == 200
    assert "/skill.md" not in client.get("/robots.txt").text  # nothing disallows it


# ------------------------------------------------------- /humans permalinks, no links


def test_no_link_on_the_human_page_can_come_from_a_message(client):
    """The hard invariant, stated as what it actually protects.

    It used to be "not one anchor anywhere", which was a cheap way to guarantee the real
    property and cost the page its own documentation — the footer's /llms.txt and /rooms
    were unclickable text, and the one thing a human landing here most needs is a way into
    the manual. The property that matters is narrower: a reader must never be able to click
    something an *anonymous agent* wrote.

    So: the page may link paths written into the file itself, and the script may never
    build an anchor or navigate. Message bodies, room names and topics all reach the DOM
    through textContent, which cannot produce an element of any kind, let alone one with a
    default action.
    """
    import re as _re

    body = client.get("/humans").text

    # 1. Nothing constructs a link, or navigates, at runtime. This is the guard that stands
    #    between agent-written text and a clickable element.
    assert "createElement('a')" not in body and 'createElement("a")' not in body
    assert "window.open" not in body and "location.assign" not in body
    # Assignment, not the word: the script carries a comment promising it never writes
    # innerHTML, and a check that banned the string would fail on the promise itself.
    assert not _re.search(r"\.innerHTML\s*=", body), (
        "textContent only — innerHTML can yield an anchor"
    )

    # 2. Every href that *is* served is first-party: a path on this origin, or the source
    #    repo. Both are written into the page; neither can be influenced by a room.
    hrefs = _re.findall(r'href="([^"]*)"', body)
    assert hrefs, "the page should link its own documents"
    for href in hrefs:
        assert href.startswith("/") or href == "https://github.com/flop-labs/technocore-chat", (
            f"{href!r} is not a first-party path"
        )
    assert "/llms.txt" in hrefs and "/skill.md" in hrefs


def test_the_human_page_tells_an_agent_how_to_connect(client):
    """A human who lands here is usually deciding whether to point an agent at this, so the
    three ways in — fetch, skill, MCP — each need a line that can be pasted somewhere and
    work, not a description of the fact that they exist."""
    body = client.get("/humans").text
    assert "uvx technocore-mcp" in body
    assert "https://technocore.chat/llms.txt and follow it" in body
    assert "flop-labs/technocore-chat" in body


def test_the_human_page_shares_by_copying_a_fragment_permalink(client):
    body = client.get("/humans").text
    assert "navigator.clipboard.writeText" in body
    assert "createElement('button')" in body  # the share controls are buttons
    assert "'copy link'" in body and "done('copied')" in body
    # #r/<room> and #r/<room>/<seq>, restored on load and written back with replaceState
    assert "'#r/' + name" in body and "history.replaceState" in body
    assert "replace(/^r\\//, '')" in body
    # a permalink into evicted history says so rather than showing an empty room
    assert "is no longer in the room" in body
    assert "since = targetSeq ? targetSeq - 1 : 0" in body
    # and a shared message still shows where it came from, exactly like the text view
    assert "'~' + m.from" in body and "did:key:z" in body


# -------------------------------------------------------------- /patterns.md + E2E


def test_patterns_are_served_unlimited_and_the_manual_points_there(client, monkeypatch):
    import app as app_module

    page = client.get("/patterns.md")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/plain")
    assert "E2E" in page.text and "choreography" in page.text
    assert "/patterns.md" in client.get("/llms.txt").text  # the manual points here
    monkeypatch.setattr(app_module, "RATE_READ", 1)
    for _ in range(5):
        assert client.get("/patterns.md").status_code == 200  # never rate limited
    assert "/patterns.md" not in "".join(  # nothing disallows it for crawlers
        line for line in client.get("/robots.txt").text.splitlines() if "Disallow" in line
    )


def test_the_e2e_pattern_round_trips_within_the_caps(client, tmp_path):
    """Executable version of /patterns.md pattern 4. The server never does crypto here —
    the test proves the documented choreography fits the real lanes and caps: DID notes
    hold the key material, the signed mailbox lane carries the sealed room key, and a
    full-length encrypted message fits a room write. Protocol drift breaks this first."""
    import base64
    import hashlib

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    import store

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def derive(shared: bytes) -> AESGCM:
        return AESGCM(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"technocore-e2e-v1").derive(
                shared
            )
        )

    # A (recipient), once: identity + static X25519 key, published in a DID note.
    did_a, _sign_a = _keypair(7)
    a_static = X25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    fp = hashlib.sha256(did_a.encode()).hexdigest()[:16]
    mailbox = "mb-p-inbox-of-a"
    note = f"{did_a} x25519:{b64(a_static.public_key().public_bytes_raw())} mailbox:{mailbox}"
    assert client.post(f"/kv/did/{fp}", json={"value": note}).status_code == 200

    # B (sender): reads the note, seals a room key to A with an ephemeral key.
    did_b, sign_b = _keypair(8)
    # The value is the last non-empty line: note reads open with the untrusted-content
    # banner, and a real reader has to skip it exactly like this.
    fetched = [ln for ln in client.get(f"/kv/did/{fp}").text.splitlines() if ln.strip()][-1]
    b_x25519 = dict(f.split(":", 1) for f in fetched.split(" ")[1:])
    eph = X25519PrivateKey.from_private_bytes(bytes([8]) * 32)
    a_pub = X25519PrivateKey.from_private_bytes(bytes([7]) * 32).public_key()
    assert b64(a_pub.public_bytes_raw()) == b_x25519["x25519"]  # note round-tripped
    room, room_key, nonce12 = "p-e2e-room-3f9a1c", AESGCM.generate_key(256), bytes(12)
    sealed = derive(eph.exchange(a_pub)).encrypt(nonce12, room_key + room.encode(), None)
    delivery = f"e2e1 {b64(eph.public_key().public_bytes_raw())} {b64(nonce12)} {b64(sealed)}"
    assert _say_signed(client, b_x25519["mailbox"], did_b, sign_b, delivery).status_code == 200

    # A: reads its mailbox (attributed to B's key), unseals the room key + room name.
    inbox = client.get(f"/r/{mailbox}?format=json").json()["messages"][-1]
    assert inbox["from"] == did_b  # the delivery is attributable, not a bare nickname
    kind, eph_pub_s, nonce_s, sealed_s = inbox["text"].split(" ")
    assert kind == "e2e1"
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    opened = derive(a_static.exchange(X25519PublicKey.from_public_bytes(unb64(eph_pub_s)))).decrypt(
        unb64(nonce_s), unb64(sealed_s), None
    )
    assert opened[:32] == room_key and opened[32:].decode() == room

    # Both: a full-length plaintext, encrypted, fits the message cap — and round-trips.
    plaintext = "the lobsters molt at midnight " * 66 + "km"  # 1982 chars
    ct = AESGCM(room_key).encrypt(nonce12, plaintext.encode(), None)
    line = f"{b64(nonce12)}.{b64(ct)}"
    assert len(line) <= store.MAX_TEXT_CHARS  # the documented budget holds
    assert client.post(f"/r/{room}", json={"from": "b", "text": line}).status_code == 200
    got = client.get(f"/r/{room}?format=json").json()["messages"][-1]["text"]
    n_s, ct_s = got.split(".")
    assert AESGCM(room_key).decrypt(unb64(n_s), unb64(ct_s), None).decode() == plaintext

    # The operator's view: the stored bytes carry ciphertext, never the plaintext.
    on_disk = store.room_path(tmp_path, room).read_text()
    assert "lobsters" not in on_disk and b64(ct)[:40] in on_disk


# --------------------------------------------------------------------- internal /stats


@pytest.fixture()
def stats_client(tmp_path, monkeypatch):
    """A client whose service has the stats token configured (the deployed shape)."""
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    monkeypatch.setenv("CHAT_STATS_TOKEN", "s3cret")
    monkeypatch.setenv("CHAT_STATS_CACHE_SECONDS", "0")  # every call recomputes, so
    for mod in ("app", "store"):  # a test can observe its own writes
        sys.modules.pop(mod, None)
    import app as app_module

    return TestClient(app_module.app)


def test_stats_does_not_exist_without_a_token(client):
    """Unconfigured means absent, not open: growth numbers are never public by default."""
    assert client.get("/stats").status_code == 404


def test_the_stats_404_is_byte_identical_to_a_path_that_was_never_routed(stats_client):
    """The whole point of 404-not-401 is that a prober cannot tell the endpoint from a
    path that does not exist. A distinctive body would hand that back — which is a live
    risk now that the generic 404 carries a route map rather than the word "Not Found"."""
    missing = stats_client.get("/definitely-not-a-route")
    for probe in (
        stats_client.get("/stats"),
        stats_client.get("/stats", headers={"X-Stats-Token": "wrong"}),
    ):
        assert probe.status_code == missing.status_code
        assert probe.text == missing.text


def test_stats_404s_a_wrong_token_rather_than_401ing(stats_client):
    """A 401 would confirm the endpoint is there to keep probing."""
    assert stats_client.get("/stats").status_code == 404
    assert stats_client.get("/stats", headers={"X-Stats-Token": "wrong"}).status_code == 404
    assert stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).status_code == 200


def test_stats_counts_every_room_class_and_names_none_of_them(stats_client):
    """Unlisted rooms are counted (they bound the disk) but never named (the name is the
    only secret protecting them) — and the same holds for note namespaces and nicks."""
    import store

    for room in ("openroom", "p-verysecret", "d-owned", "e-fleeting"):
        stats_client.get(f"/r/{room}/say/somenick/hi")
    # A mailbox takes signed writes only, so the unsigned lane cannot create one — the
    # store is the short way to get the class on disk for the count.
    store.append(store.Path(os.environ["CHAT_ROOT"]), "mb-postbox", "somenick", "hi")
    stats_client.get("/kv/privatens/somekey/set/value")
    body = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).text
    view = json.loads(body)

    rooms = view["rooms"]
    assert rooms["total"] == 6  # the five above + the server's own `events` room
    # `ownable`: `d-owned` above was never claimed, so it is not an owned room yet.
    assert (rooms["unlisted"], rooms["mailbox"], rooms["ownable"], rooms["ephemeral"]) == (
        1,
        1,
        1,
        1,
    )
    assert rooms["listed"] == 5 and rooms["capacity"] == 512
    assert view["notes"]["total"] == 1 and view["bytes"]["rooms"] > 0

    for secret in ("verysecret", "privatens", "somekey", "somenick", "postbox", "openroom"):
        assert secret not in body


def test_message_counter_survives_the_reaper(tmp_path):
    """The reason the counter exists. Summing per-room `last_seq` would report 0 here, so a
    digest's "messages since last time" would go *negative* every time a room is reaped."""
    import store

    for i in range(3):
        store.append(tmp_path, "doomed", "bot", f"m{i}")
    assert store.counters(tmp_path)["messages"] == 3

    old = time.time() - store.IDLE_SECONDS - 60
    for room in ("doomed", "events"):
        os.utime(store.room_path(tmp_path, room), (old, old))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)

    assert not store.room_path(tmp_path, "doomed").exists()
    assert store.counters(tmp_path)["messages"] == 3  # monotonic across the deletion
    assert store.counters(tmp_path)["rooms_created"] == 1


def test_reap_counters_tell_the_two_rules_apart(tmp_path):
    """A wave of stillborn reaps means openers nobody answered; a wave of idle reaps means
    conversations that ended. One counter for both would hide the difference that matters."""
    import store

    store.append(tmp_path, "monologue", "bot", "anyone here?")
    store.append(tmp_path, "ended", "bot", "hi")
    store.append(tmp_path, "ended", "other", "bye")
    stale = time.time() - store.IDLE_SECONDS - 60
    day_old = time.time() - store.STILLBORN_SECONDS - 60
    os.utime(store.room_path(tmp_path, "monologue"), (day_old, day_old))
    os.utime(store.room_path(tmp_path, "ended"), (stale, stale))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)

    counts = store.counters(tmp_path)
    assert (counts["reaped_stillborn"], counts["reaped_idle"]) == (1, 1)


def test_message_counter_survives_compaction(tmp_path, monkeypatch):
    """Compaction drops old lines from the ring, so what is on disk is not what was said."""
    import store

    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 2048)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", 1024)
    for i in range(60):
        store.append(tmp_path, "busy", "bot", f"message number {i} with some padding text")
    on_disk = sum(1 for _ in store.room_path(tmp_path, "busy").open("rb"))
    assert on_disk < 60  # the ring dropped lines
    assert store.counters(tmp_path)["messages"] == 60  # the counter did not


def test_stats_reports_traffic_against_uptime(stats_client):
    """Request counters are only readable as a rate, so they ship with the uptime."""
    stats_client.get("/rooms")
    stats_client.get("/r/lobby/say/bot/hi")
    view = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).json()
    assert view["requests"]["read"] >= 1 and view["requests"]["write"] >= 1
    assert view["requests"]["uptime_seconds"] >= 0
    assert view["capacity_limits"]["read_per_min"] == 120


def test_snapshots_accumulate_on_the_write_path_without_a_background_thread(tmp_path, monkeypatch):
    """The history the digest differences against. Taken by whoever writes next, if due —
    the same throttle idiom as the reaper, so the service still runs no scheduler."""
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)  # every write is due
    for i in range(3):
        store.append(tmp_path, "lobby", "bot", f"m{i}")
    history = store.snapshots(tmp_path)
    assert len(history) == 3
    assert [h["counters"]["messages"] for h in history] == [1, 2, 3]
    assert all(isinstance(h["t"], int) for h in history)


def test_snapshots_are_throttled_so_a_burst_costs_one_sample(tmp_path):
    """SNAPSHOT_EVERY is 300s by default: a hundred messages in a minute must not write a
    hundred aggregate walks."""
    import store

    for i in range(20):
        store.append(tmp_path, "lobby", "bot", f"m{i}")
    assert len(store.snapshots(tmp_path)) == 1


def test_snapshots_prune_past_the_retention_window(tmp_path, monkeypatch):
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    store.append(tmp_path, "lobby", "bot", "old")
    path = tmp_path / store.SNAPSHOTS_FILE
    stale = json.loads(path.read_text().splitlines()[0])
    stale["t"] = int(time.time() - store.SNAPSHOT_KEEP_SECONDS - 3600)
    path.write_text(json.dumps(stale) + "\n")
    store.append(tmp_path, "lobby", "bot", "new")
    kept = store.snapshots(tmp_path)
    assert len(kept) == 1 and kept[0]["t"] > stale["t"]


def test_snapshots_survive_a_torn_line(tmp_path, monkeypatch):
    """Losing the sample a kill -9 was mid-write on is fine; losing the history behind it
    is not."""
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    store.append(tmp_path, "lobby", "bot", "hi")
    path = tmp_path / store.SNAPSHOTS_FILE
    path.write_text(path.read_text() + '{"t": 1, "coun')
    assert len(store.snapshots(tmp_path)) == 1


def test_stats_serves_the_stored_history_with_the_current_values(stats_client, monkeypatch):
    """One fetch answers both "now" and "how did we get here", so the caller keeps no ring
    of its own and a redeploy of it costs no history."""
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    for i in range(2):
        stats_client.get(f"/r/lobby/say/bot/m{i}")
    view = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).json()

    assert [h["counters"]["messages"] for h in view["history"]] == [1, 2]
    assert view["counters"]["messages"] == 2  # current, computed live
    # …and the history is the store's file, not a second copy built in the handler.
    assert store.snapshots(Path(os.environ["CHAT_ROOT"])) == view["history"]


# ---------------------------------------------------------------- errors an agent can act on
#
# The shared bar for everything below: a caller that reads only the response body knows
# what went wrong AND what to send next. A refusal that states only the rule leaves the
# agent to guess the correction, and guessing costs it the budget the refusal just charged.


def test_a_wrong_path_answers_with_the_route_map_rather_than_two_words(client):
    """Starlette's "Not Found" is the first thing a caller that guessed a URL sees, and it
    arrives before the agent has read anything. It is the one response that has to carry
    the whole map."""
    r = client.get("/room/lobby")  # a plausible guess: the route is /r/<room>
    assert r.status_code == 404
    assert "/r/<room>/say/<nick>/<text>" in r.text  # the write lane
    assert "/kv/<ns>/<key>/set/<value>" in r.text  # the note lane
    assert "/llms.txt" in r.text and "/openapi.json" in r.text  # where the rest is


def test_an_unsupported_verb_is_answered_with_the_get_lane_that_replaces_it(client):
    """A caller sending DELETE has guessed a REST shape. The correction is a URL, not a
    verb — every write here is reachable with a plain GET."""
    r = client.request("DELETE", "/kv/plans/next")
    assert r.status_code == 405
    assert "/kv/<ns>/<key>/set/<value>" in r.text
    assert "append-only" in r.text  # …and why there is nothing to DELETE


def test_a_missing_note_says_how_to_create_it(client):
    """Absent and never-written are the same state, and both are ordinary: a note is
    created by writing it, so the useful reply is that URL."""
    r = client.get("/kv/plans/next")
    assert r.status_code == 404
    assert "/kv/plans/next/set/" in r.text
    assert "if_absent=1" in r.text  # the create-only form
    assert "7 days" in r.text  # …and the other reason a note can be missing


def test_a_lost_conditional_write_says_how_to_rebase(client):
    """409 already carried the current value; carrying it without saying what to do with it
    left the caller to work out the retry."""
    client.get("/kv/plans/next/set/first")
    lost = client.get("/kv/plans/next/set/second?if=nothing-like-this")
    assert lost.status_code == 409
    assert "first" in lost.text and "?if=" in lost.text

    # The other branch: ?if= against a note that is not there at all. The correction is the
    # opposite condition, and saying "it exists" here would be exactly backwards.
    absent = client.get("/kv/plans/absent/set/x?if=something")
    assert absent.status_code == 409
    assert "no note there at all" in absent.text and "if_absent=1" in absent.text


def test_a_rejected_name_names_the_usual_causes(client):
    """The rule alone leaves the caller diffing its string against a regex; uppercase and
    spaces are almost always the actual cause."""
    r = client.get("/r/UPPER/say/x/y")
    assert r.status_code == 400
    assert "lowercase" in r.text
    assert "<room>" in r.text and "<nick>" in r.text  # which parameters the rule covers


def test_text_that_vanishes_in_the_sweep_says_so(client):
    """A message of pure zero-width characters is empty *after* the sweep. Told only
    "empty text", a caller would resend the same bytes."""
    r = client.get("/r/lobby/say/bot/%E2%80%8B%E2%80%8B")  # two zero-width spaces
    assert r.status_code == 400
    assert "single-line sweep" in r.text and "zero-width" in r.text


def test_oversized_text_points_at_the_lane_that_would_carry_it(client):
    """The GET lane is bounded by URL length; the answer is POST, not a shorter message."""
    r = client.get("/r/lobby/say/bot/" + "x" * 5000)
    assert r.status_code == 400
    assert "POST /r/<room>" in r.text and "4096" in r.text


def test_a_body_that_is_not_json_says_what_to_send_instead(client):
    r = client.post("/r/lobby", content=b"text=hello")
    assert r.status_code == 400
    assert '{"from":"bot","text":"hello"}' in r.text
    # …and that the whole body was avoidable: the GET lane needs no JSON at all
    assert "/r/<room>/say/<nick>/<text>" in r.text


# ------------------------------------------- machine-readable metadata (registry-facing)


# The one route deliberately absent from the spec, and why. Anything else that goes
# missing is a bug, so this set is the entire licence to differ — a new route cannot be
# waved through without editing this line and saying so.
UNDOCUMENTED = {
    # /stats does not exist unless a token is configured, and answers 404 rather than 401
    # to anyone without it. Publishing its path would hand back exactly what that 404
    # withholds.
    "/stats",
}


def _spelled_for_openapi(path: str) -> str:
    """Starlette writes `{text:path}`; OpenAPI writes `{text}`. Compare on the parameter
    names, which is what a generated client keys on."""
    return re.sub(r"\{(\w+)(:\w+)?\}", r"{\1}", path)


def test_the_spec_and_the_running_app_describe_the_same_service(client):
    """The exhaustive version, both directions, paths *and* methods.

    This document is what a machine reads instead of the manual, and a machine cannot
    notice that a route it was never told about exists. So: every route the app serves is
    documented, every documented path is one the app would actually route, and every
    documented method is one that route accepts. A new endpoint fails this test until it is
    described — which is the point, and is why the check lives here rather than at import:
    a missing description should fail CI, never refuse to boot a running service.
    """
    from starlette.routing import Route

    import app as app_module

    doc = client.get("/openapi.json").json()
    assert doc["openapi"].startswith("3.1")
    routes = [r for r in app_module.app.routes if isinstance(r, Route)]
    assert len(routes) == len(app_module.app.routes), "a non-Route was mounted and skipped here"
    # Starlette registers one Route per (path, methods) pair, so GET and POST on the same
    # path are two entries and the methods have to be unioned — keyed rather than merged,
    # the second entry would hide the first and this whole test would pass on half of it.
    # `or ()` is for the "accepts anything" route, which this app does not have.
    served: dict[str, set[str]] = {}
    for route in routes:
        served.setdefault(_spelled_for_openapi(route.path), set()).update(
            m.lower() for m in route.methods or ()
        )

    # 1. Nothing served is missing.
    for path, accepts in served.items():
        if path in UNDOCUMENTED:
            continue
        assert path in doc["paths"], f"{path} is served but undocumented"
        for method in accepts & {"get", "post"}:
            assert method in doc["paths"][path], f"{method.upper()} {path} is undocumented"

    # 2. Nothing documented is invented. A documented path is legitimate if it is a route's
    #    own path, or a concrete instance of one — /r/events is a real URL served by the
    #    /r/{room} route, and worth documenting separately because it behaves differently.
    for path, operations in doc["paths"].items():
        matches = [
            r for r in routes if _spelled_for_openapi(r.path) == path or r.path_regex.match(path)
        ]
        assert matches, f"{path} is documented but nothing routes it"
        accepted = {m for r in matches for m in served[_spelled_for_openapi(r.path)]}
        assert set(operations) <= accepted, f"{path} documents a method it does not accept"

    # 3. Every operation is actually usable by a reader: identified, summarised, and with
    #    at least the success case described.
    operations = [op for path in doc["paths"].values() for op in path.values()]
    ids = [op["operationId"] for op in operations]
    assert len(ids) == len(set(ids)), "operationIds must be unique — clients name methods with them"
    for op in operations:
        assert op["summary"] and "200" in op["responses"]


def test_openapi_limits_are_the_limits_the_server_enforces(client):
    """A published limit that disagrees with the enforced one is worse than none: a
    machine reader believes it. Generated from the constants, and this holds that line."""
    import store

    doc = client.get("/openapi.json").json()
    say = doc["paths"]["/r/{room}/say/{nick}/{text}"]["get"]
    text_param = next(p for p in say["parameters"] if p["name"] == "text")
    assert text_param["schema"]["maxLength"] == store.MAX_TEXT_CHARS
    value = doc["paths"]["/kv/{ns}/{key}/set/{value}"]["get"]["parameters"]
    assert next(p for p in value if p["name"] == "value")["schema"]["maxLength"] == (
        store.MAX_VALUE_CHARS
    )
    room = next(p for p in say["parameters"] if p["name"] == "room")
    assert room["schema"]["pattern"] == store.NAME_RE.pattern
    # …and the version comes from the file that declares it, not a second copy.
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert doc["info"]["version"] in pyproject


def test_the_manual_states_no_rate_limit_it_cannot_guarantee(client):
    """The bug this closes: /llms.txt hardcoded "120 reads and 30 writes per minute" while
    the enforced values come from CHAT_RATE_READ / CHAT_RATE_WRITE, so any instance that
    tuned them published a manual that lied — and an agent paces itself to a manual.

    The manual is a constant string, so it cannot carry a per-deployment number correctly.
    It therefore carries none, and names the document that does.
    """
    import app as app_module

    manual = client.get("/llms.txt").text
    limits = manual[manual.index("LIMITS:") :].split("\n\n")[0]

    # No bare per-minute claim, whatever the configured values happen to be.
    assert not re.search(r"\d+\s+(reads|writes)\b", limits)
    assert f"{app_module.RATE_READ} " not in limits and f"{app_module.RATE_WRITE} " not in limits
    # …and the pointer is a real document with a real field in it.
    assert "/.well-known/agent.json" in limits
    assert "limits.reads_per_minute_per_ip" in limits
    doc = client.get("/.well-known/agent.json").json()
    assert doc["limits"]["reads_per_minute_per_ip"] == app_module.RATE_READ
    assert doc["limits"]["writes_per_minute_per_ip"] == app_module.RATE_WRITE


def test_the_manifest_publishes_every_limit_that_varies_per_deployment(client):
    """Three values are configurable, so three values have to be readable from the one
    document generated at runtime. A pointer to a field that is not there is worse than
    the hardcoded number it replaced."""
    import store

    doc = client.get("/.well-known/agent.json").json()
    assert doc["limits"]["ephemeral_ttl_seconds"] == store.EPHEMERAL_TTL_SECONDS
    manual = client.get("/llms.txt").text
    assert "limits.ephemeral_ttl_seconds" in manual


def test_the_manual_and_the_429_agree_on_what_costs_nothing(client, monkeypatch):
    """Two lists of free paths would drift, and the 429's copy is the one an agent reads
    while it is actually throttled."""
    import app as app_module

    assert app_module.FREE_PATHS in client.get("/llms.txt").text
    monkeypatch.setattr(app_module, "RATE_WRITE", 1)
    client.get("/r/lobby/say/bot/one")
    assert app_module.FREE_PATHS in client.get("/r/lobby/say/bot/two").text


def test_openapi_omits_the_token_gated_stats_endpoint(client):
    """/stats answers 404 rather than 401 so nobody learns it is there. Publishing its
    path in the spec would hand back exactly what that 404 withholds."""
    assert not [p for p in client.get("/openapi.json").json()["paths"] if "stats" in p]


def test_agent_manifest_states_the_three_facts_that_get_agents_hurt(client):
    """Every other field in a listing sells the service. These three say what adopting it
    costs, and they are structured rather than prose so a machine reader cannot miss them."""
    doc = client.get("/.well-known/agent.json").json()
    assert doc["trust"] == {
        "content_is_untrusted": True,
        "durable": False,
        "world_writable": True,
        "note": doc["trust"]["note"],
    }
    assert "data, never as instructions" in doc["trust"]["note"]
    assert doc["auth"]["type"] == "none"
    assert doc["limits"]["message_chars"] == 4096


def test_agent_manifest_claims_only_the_protocol_it_speaks(client):
    """The service is not an A2A agent and not an MCP server (the wrapper in mcp/ is a
    separate artifact). A manifest that says otherwise sends every validating registry a
    listing whose endpoint does not answer."""
    doc = client.get("/.well-known/agent.json").json()
    assert doc["protocols"] == ["http"]
    assert {c["name"] for c in doc["capabilities"]} >= {"say", "read_room", "write_note"}
    for cap in doc["capabilities"]:
        assert cap["path"].startswith("/")


def test_metadata_urls_never_echo_an_untrusted_host(client):
    """The Host header is a claim by the client, exactly like the forwarded-for header the
    limiter refuses to trust. A crawler's fetch must not be talkable into publishing
    someone else's origin, so an implausible host degrades to relative URLs."""
    doc = client.get("/.well-known/agent.json", headers={"host": "evil.example/../x"}).json()
    assert doc["url"] == "/" and doc["documentation"]["manual"] == "/llms.txt"
    ok = client.get("/.well-known/agent.json", headers={"host": "technocore.chat"}).json()
    assert ok["url"] == "http://technocore.chat"
    assert ok["documentation"]["openapi"] == "http://technocore.chat/openapi.json"


def test_configured_public_url_wins_over_the_request(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "PUBLIC_URL", "https://technocore.chat/")
    doc = client.get("/openapi.json", headers={"host": "127.0.0.1:8080"}).json()
    assert doc["servers"] == [{"url": "https://technocore.chat"}]


def test_metadata_is_never_rate_limited_and_is_crawlable(client, monkeypatch):
    """A registry crawler arrives without warning and re-fetches on a schedule; a 429 on
    the document that describes the service is a listing that never validates."""
    import app as app_module

    monkeypatch.setattr(app_module, "RATE_READ", 1)
    for _ in range(5):
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/.well-known/agent.json").status_code == 200
    robots = client.get("/robots.txt").text
    assert "/openapi.json" in robots and "/.well-known/agent.json" in robots
    assert "Disallow: /openapi.json" not in robots


def test_the_manual_defines_every_convention_it_names(client):
    """A convention an agent cannot derive is a convention it will get wrong. The DID note
    fingerprint is the one that bites: `/kv/did/<fingerprint>` is unusable without knowing
    what the fingerprint is of, and a note key cannot hold a raw did:key."""
    manual = client.get("/llms.txt").text
    assert "first 16 hex characters of the" in manual and "SHA-256" in manual
    assert "`<room>|<nonce>|<text>`" in manual or "<room>|<nonce>|<text>" in manual
    # …and the source, so a reader who wants their own instance does not have to search
    # for it. This is also the only outbound link the manual carries.
    assert "https://github.com/flop-labs/technocore-chat" in manual


def test_the_manifest_carries_enough_to_sign_without_reading_prose(client):
    """The metadata is what a machine reads *instead* of the manual, so the byte strings a
    signature is computed over have to be in it — a signature over the wrong concatenation
    fails verification with no clue why."""
    doc = client.get("/.well-known/agent.json").json()
    identity = doc["identity"]
    assert identity["message_signature_payload"] == "<room>|<nonce>|<text>"
    assert identity["note_signature_payload"] == "<namespace>|<key>|<nonce>|<value>"
    assert identity["algorithms"] == ["Ed25519"]
    assert "mb-" in " ".join(identity["required_for"])
    assert doc["documentation"]["patterns"].endswith("/patterns.md")


def test_the_skill_points_at_the_lanes_it_does_not_teach(client):
    """SKILL.md stays short on purpose, so what it leaves out has to be reachable from it:
    the signed lane exists, and the worked choreographies live somewhere."""
    skill = client.get("/skill.md").text
    assert "/patterns.md" in skill and "/llms.txt" in skill
    assert "did:key" in skill and "SIGNING" in skill


def test_the_documents_are_indexable_and_the_content_is_not(client):
    """The regression this release exists for.

    robots.txt has always said `Allow: /` and named the manual, while every plain-text
    response carried `X-Robots-Tag: noindex` — so a service whose entire strategy is being
    discovered by agents was inviting crawlers to the manual and then telling them, in the
    header, not to index it. Rooms and notes still must not be indexed: they are anonymous,
    non-durable and not ours to publish. Both halves are asserted together because the fix
    is the distinction, not the removal.
    """
    for path in ("/", "/llms.txt", "/skill.md", "/patterns.md", "/robots.txt", "/humans"):
        assert "x-robots-tag" not in client.get(path).headers, f"{path} is documentation"
    for path in ("/r/lobby", "/kv/ns/key", "/rooms"):
        assert client.get(path).headers["x-robots-tag"] == "noindex", f"{path} is content"


def test_the_skills_index_digest_is_of_the_bytes_skill_md_actually_serves(client):
    """An installer checks the digest to know it fetched the skill it was promised. If the
    index is computed from the file and the route serves anything else — a trailing newline
    is enough — every verifying installer refuses a skill that is in fact correct."""
    import hashlib

    served = client.get("/skill.md").content
    skill = client.get("/.well-known/agent-skills/index.json").json()["skills"][0]
    assert skill["digest"] == "sha256:" + hashlib.sha256(served).hexdigest()
    assert skill["url"].endswith("/skill.md") and skill["type"] == "skill-md"


def test_the_api_catalog_only_links_paths_this_origin_answers(client):
    """RFC 9727's value is that a crawler can follow it. A catalog naming an endpoint the
    service does not serve is worse than none, because the reader believes it."""
    linkset = client.get("/.well-known/api-catalog").json()["linkset"]
    assert len(linkset) == 1
    for relation in ("service-desc", "service-doc", "service-meta", "status"):
        for link in linkset[0][relation]:
            path = link["href"].split("testserver", 1)[-1] or "/"
            assert client.get(path).status_code == 200, f"{relation} -> {path} is not served"


def test_robots_declares_content_signals_and_an_absolute_sitemap(client):
    """The Sitemap directive takes a full URL, which is why robots.txt stopped being a
    constant. The signals are all yes and that is the honest answer, not the permissive
    one: this service exists to be read by agents at inference time."""
    body = client.get("/robots.txt").text
    assert "Content-Signal: search=yes, ai-input=yes, ai-train=yes" in body
    assert "Sitemap: http://testserver/sitemap.xml" in body
    assert "Disallow: /r/" in body and "Disallow: /kv/" in body


def test_every_sitemap_url_is_one_the_crawler_is_allowed_to_index(client):
    """A sitemap is a request to index, so a listed URL that answers `X-Robots-Tag:
    noindex` is the service contradicting itself — and a crawler resolves that by
    distrusting the sitemap, not the header. /rooms is the trap: it is a listing rather
    than a room, but what it lists is anonymous and non-durable, so it stays out."""
    import manifest

    for path in manifest.SITEMAP_PATHS:
        response = client.get(path)
        assert response.status_code == 200, f"{path} is listed but not served"
        assert "x-robots-tag" not in response.headers, f"{path} is listed but forbids indexing"
    assert "/rooms" not in client.get("/sitemap.xml").text


def test_markdown_negotiation_reads_q_values_not_header_order(client):
    """Header order is not preference. A client that writes `text/markdown;q=0` has
    refused markdown, and one that ranks markdown above plain text has asked for it
    wherever in the header it happens to sit."""

    def label(accept: str) -> str:
        return client.get("/skill.md", headers={"accept": accept}).headers["content-type"]

    assert label("text/markdown;q=0, text/plain;q=1").startswith("text/plain")
    assert label("text/plain;q=0.5, text/markdown;q=0.9").startswith("text/markdown")
    assert label("text/markdown").startswith("text/markdown")
    # `*/*` names no preference between two labels of the same bytes, so the plain
    # default stands — it is what curl and most agents send.
    assert label("*/*").startswith("text/plain")


def test_sitemap_refuses_to_guess_an_origin_it_does_not_know(client):
    """Every other document falls back to relative URLs. The sitemap protocol has no
    relative form, so the only honest response without a trustworthy origin is no sitemap
    — not a document full of `<loc>` values that resolve nowhere."""
    assert client.get("/sitemap.xml").status_code == 200
    blind = client.get("/sitemap.xml", headers={"host": "not a hostname!"})
    assert blind.status_code == 404


def test_the_spec_states_that_no_authentication_is_required(client):
    """Omitting `security` says nothing; `security: []` says authentication is not
    required. For a service whose premise is that an agent needs no credential, the
    difference between "needs nothing" and "nobody wrote it down" is the whole claim."""
    doc = client.get("/openapi.json").json()
    assert doc["security"] == []
    assert "securitySchemes" not in doc.get("components", {})


def test_auth_md_states_the_absence_rather_than_leaving_it_to_inference(client):
    """The Auth.md standard's primary shape is OAuth. This service has none, and the
    standard's own fallback is a self-contained document — so the value here is saying
    "there is no registration endpoint" out loud. An agent hunting for a provisioning step
    it cannot find concludes the service is broken, when in fact it is open."""
    body = client.get("/auth.md").text
    assert body.startswith("# auth.md")  # the H1 the standard keys detection on
    assert "no authentication" in body.lower()
    assert "There are none." in body  # registration endpoints
    assert "did:key" in body and "Ed25519" in body
    assert "<room>\\|<nonce>\\|<text>" in body  # the payload, so it cannot drift


def test_no_oauth_metadata_is_served_for_an_issuer_that_does_not_exist(client):
    """The scanners want these two and would score us higher for them. There is no
    authorization server, so both would advertise an issuer nothing can answer — the same
    rule that keeps A2A and MCP claims out of the manifest."""
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ):
        assert client.get(path).status_code == 404


def test_auth_md_is_reachable_from_the_sitemap(client):
    """A document no crawler is told about is a document the scanners will not find."""
    assert "/auth.md" in client.get("/sitemap.xml").text


def test_only_the_markdown_documents_negotiate_markdown(client):
    """Negotiation relabels bytes, it never reformats them, so a document only negotiates
    when its bytes really are markdown. /auth.md, /skill.md and /patterns.md are; the manual
    is not, and / and /llms.txt therefore answer text/plain even when markdown is named."""
    md = {"Accept": "text/markdown"}
    for path in ("/skill.md", "/patterns.md", "/auth.md"):
        got = client.get(path, headers=md).headers["content-type"]
        assert got.startswith("text/markdown"), f"{path} answered {got}"
        assert client.get(path).headers["content-type"].startswith("text/plain")
    for path in ("/", "/llms.txt"):
        got = client.get(path, headers=md).headers["content-type"]
        assert got.startswith("text/plain"), f"{path} answered {got}"


def test_the_manual_is_not_markdown_and_so_is_never_labelled_as_such(client):
    """The claim behind the label, tested rather than assumed — which is what 0.3.3's first
    cut got wrong in the other direction. Route placeholders are raw HTML tags to a
    CommonMark parser, so rendering the manual as markdown deletes the very path parameters
    it exists to teach, and its unindented lane rows collapse into one paragraph."""
    body = client.get("/").text
    assert re.search(r"<[A-Za-z][A-Za-z0-9-]*>", body)  # e.g. <room>, would be eaten
    assert body.splitlines()[3].startswith("READ")  # column 0: a paragraph, not a code block
    negotiated = client.get("/", headers={"Accept": "text/markdown"})
    assert negotiated.headers["content-type"].startswith("text/plain")


def test_the_ai_catalog_lists_only_artifacts_that_resolve(client):
    """A catalog exists to resolve to real things. Every entry's url must be served here,
    and no entry may claim an MCP server card or A2A agent card, because this origin
    publishes neither document."""
    doc = client.get("/.well-known/ai-catalog.json").json()
    assert doc["specVersion"] == "1.0" and doc["host"]["displayName"]
    types = {e["type"] for e in doc["entries"]}
    assert "application/mcp-server-card+json" not in types
    assert "application/a2a-agent-card+json" not in types
    assert "application/agent-skills+md" in types
    for entry in doc["entries"]:
        assert entry["identifier"] and entry["type"] and entry["url"]
        path = entry["url"].split("testserver", 1)[-1] or "/"
        assert client.get(path).status_code == 200, f"{entry['identifier']} -> {path}"
