"""Run: uv run --group dev python -m pytest tests"""

import json
import re
import time
from pathlib import Path

import _client
from starlette.testclient import TestClient

client = _client.client  # the shared TestClient fixture


def test_a_fractional_wait_is_honoured_rather_than_silently_dropped(client, monkeypatch):
    """`?wait=` is published as `type: number` and the poll interval is half a second, so
    `wait=0.5` is the shortest wait that can return anything — the constant's own comment
    calls it the useful floor. It was int-parsed, so every fractional value became no wait
    at all, and the caller got an immediate empty reply indistinguishable from an idle
    room. Review catch on #40.
    """
    import app as app_module
    import config

    assert app_module._seconds("0.5") == 0.5
    assert app_module._seconds("2.5") == 2.5
    # Junk, negative and absent all mean "do not wait" rather than raising.
    for junk in (None, "", "abc", "-1", "nan", "²"):
        assert app_module._seconds(junk) == 0.0, junk
    # The ceiling is applied here, so it cannot be enforced in one caller and forgotten in
    # another. Infinity is just an over-large number.
    with config.override(MAX_WAIT=1.0):
        assert app_module._seconds("10") == 1.0
        assert app_module._seconds("inf") == 1.0

    # End to end: a fractional wait really does hold the connection open and then return.
    started = time.monotonic()
    r = client.get("/r/quiet?since=1&wait=0.5")
    assert r.status_code == 200 and time.monotonic() - started >= 0.4


def test_an_instance_with_a_sub_second_ceiling_still_polls(client, monkeypatch):
    """The sharp end of the same bug: below a one-second ceiling every schema-conforming
    positive wait int-parsed to zero, so the feature the document advertised could not be
    used at all on that instance."""
    import app as app_module
    import config

    with config.override(MAX_WAIT=0.5):
        # The ceiling is a clamp, not a refusal, so it is published as prose on the `wait`
        # parameter and as a number in `limits.long_poll_seconds` rather than as a
        # `maximum` the handler never enforced (docs/design.md §3.5).
        published = client.get("/.well-known/agent.json").json()["limits"]["long_poll_seconds"]
        assert published == 0.5
        wait = next(
            p
            for p in client.get("/openapi.json").json()["paths"]["/r/{room}"]["get"]["parameters"]
            if p["name"] == "wait"
        )
        assert "clamped to 0.5" in wait["description"]
        # The largest value the service advertises is a wait the server actually takes.
        assert app_module._seconds(str(published)) == 0.5


def test_the_body_cap_holds_when_nothing_declares_a_length(client):
    """Two bounds, and only one of them was ever reached. `await request.body()` buffers
    the whole upload before any size check, so a large POST was an OOM against a 128 MiB
    container; the Content-Length refusal fixes the honest case and the streaming cap
    fixes the rest. A chunked request declares no length, so the second bound is the only
    one that applies there — and every oversize test until now sent a declared length,
    because the test client computes one for you.
    """
    import app as app_module

    def chunked():
        for _ in range(4):
            yield b"x" * (app_module.MAX_BODY // 2)

    streamed = client.post("/r/lobby", content=chunked())
    assert streamed.status_code == 413
    assert "the stream passed it before it ended" in streamed.text

    declared = client.post("/r/lobby", content=b"x" * (app_module.MAX_BODY + 1))
    assert declared.status_code == 413
    assert f"your Content-Length said {app_module.MAX_BODY + 1} bytes" in declared.text

    # Both bodies name the cap, because a caller that just lost an upload needs the number.
    for response in (streamed, declared):
        assert f"the cap is {app_module.MAX_BODY} bytes" in response.text


def test_rate_limit_is_actionable_without_headers(client, monkeypatch):
    import config

    with config.override(RATE_WRITE=4):
        codes = [client.get(f"/r/lobby/say/bot/m{i}").status_code for i in range(6)]
        assert codes[:4] == [200] * 4 and codes[4:] == [429, 429]
        r = client.get("/r/lobby/say/bot/again")
        assert r.headers["retry-after"].isdigit()
        # the wait is in the body too: harness webfetch shows page text, not headers
        assert "retry after:" in r.text and "429 rate limited" in r.text
        # …and it is the right order of magnitude. A bucket refilling at 4/min hands back a
        # token in ~15s; the arithmetic that produces that is one character from reporting
        # four minutes, and an agent that believes it sleeps through its own work.
        assert 1 <= int(r.headers["retry-after"]) <= 60 // 4 + 1
        assert f"retry after: {r.headers['retry-after']}s" in r.text  # header and body agree
        # the manual stays reachable while throttled, so a limited agent can learn to back
        # off
        assert client.get("/llms.txt").status_code == 200
        assert client.get("/r/lobby").status_code == 200  # reads have their own budget


def test_every_rate_limited_route_returns_the_same_recovery_plan(client, monkeypatch):
    """A new route must not accidentally become a free validation/IO oracle, and an agent
    that only sees the body must get the same useful next step whichever lane it exhausted.

    The first signed-note call is deliberately invalid: signature verification is work an
    attacker can amplify, so malformed signed traffic has to spend its token before parsing.
    """
    import app as app_module
    import config

    with config.override(RATE_READ=1, RATE_WRITE=1):
        signed = "/kv/room-owners/d-rate/set-signed/not-a-did/not-a-signature/1/not-a-did"
        routes = (
            ("read", "room read", lambda: client.get("/r/rate-tail")),
            ("read", "note read", lambda: client.get("/kv/rate/missing")),
            ("read", "note listing", lambda: client.get("/kv/rate")),
            (
                "write",
                "room POST",
                lambda: client.post("/r/rate-post", json={"from": "bot", "text": "hi"}),
            ),
            ("write", "note GET write", lambda: client.get("/kv/rate/get/set/value")),
            ("write", "signed note write", lambda: client.get(signed)),
            ("write", "note POST", lambda: client.post("/kv/rate/post", json={"value": "v"})),
        )

        for kind, label, call_route in routes:
            app_module._buckets.clear()
            first = call_route()
            refused = call_route()
            assert first.status_code != 429, label
            assert refused.status_code == 429, label
            assert f"the {kind} budget" in refused.text, label
            assert "retry after:" in refused.text, label
            assert "still open:" in refused.text, label
            assert "prefer &wait=10 to tight polling" in refused.text, label
            assert "/.well-known/agent.json" in refused.text, label


def test_the_429_names_the_budget_the_manual_deliberately_does_not(client, monkeypatch):
    """The numbers are per deployment, so no document states them as prose. That only works
    if the responses carry them — otherwise removing them from the manual just loses them.
    """
    import app as app_module
    import config

    with config.override(RATE_WRITE=2):
        for i in range(3):
            r = client.get(f"/r/lobby/say/bot/m{i}")
        assert r.status_code == 429
        assert "(2/min)" in r.text  # the enforced number, not a documented one
        assert "one token every 30s" in r.text  # …and the refill, as a sleep
        # what still works while throttled, and where to read the limits up front
        assert "reads are a separate budget" in r.text
        assert "limits.writes_per_minute_per_ip" in r.text
        # The poll advice names the ceiling this instance enforces, not a hardcoded 10 —
        # the same reason the manual states no rate limit it cannot guarantee.
        assert f"&wait={app_module.MAX_WAIT:g}" in r.text

        # The read bucket is the other half, and it is the one an agent hits first.
        # `other` is computed from `kind`, so a 429 that names the wrong budget as
        # "still open" sends the caller straight back into the bucket it just emptied.
        with config.override(RATE_READ=1):
            for _ in range(2):
                read = client.get("/r/lobby")
            assert read.status_code == 429
            assert "the read budget for your IP (1/min) is spent" in read.text
            assert "writes are a separate budget" in read.text
            assert "limits.reads_per_minute_per_ip" in read.text


def test_a_zero_rate_limit_refuses_rather_than_crashing(monkeypatch, tmp_path):
    """The bucket arithmetic divides by the limit, so CHAT_RATE_WRITE=0 turned every write
    into a 500 on the limiter itself. Floored at import instead — and the floor has to
    arrive in the app module the service actually runs, so this boots the real chain
    (app importing config importing the environment) in a fresh interpreter: no
    sys.modules surgery in this process, and unlike a re-exec of config alone it fails
    if app ever stops propagating the value (review: PR #59)."""
    import os
    import subprocess
    import sys

    src = repr(str(Path(__file__).resolve().parents[2] / "src"))
    boot = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {src}); import app; print(app.RATE_WRITE)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "CHAT_ROOT": str(tmp_path), "CHAT_RATE_WRITE": "0"},
    )
    assert boot.returncode == 0, boot.stderr
    assert boot.stdout.strip() == "1"

    import app as app_module
    import config

    app_module._buckets.clear()
    with config.override(ROOT=tmp_path, RATE_WRITE=1):
        assert TestClient(app_module.app).get("/r/lobby/say/bot/hi").status_code == 200


def test_every_path_the_429_calls_free_really_is_free(client, monkeypatch):
    """Advice that fails at the moment it is taken is worse than no advice: a throttled
    agent following this list must not meet a second 429."""
    import app as app_module
    import config

    with config.override(RATE_READ=1):
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
    import config

    with config.override(RATE_READ=8):
        assert "# budget:" not in client.get("/r/lobby").text
        for _ in range(5):
            client.get("/r/lobby")
        assert "# budget: 1 of 8 reads left" in client.get("/r/lobby").text


def test_new_rooms_are_budgeted_per_ip_and_say_when_to_retry(client, monkeypatch):
    """The room cap bounds the service; this bounds how much of it one caller can take.

    Without it, MAX_ROOMS is not a cap so much as a race: at the write limit a single IP
    exhausts it in hours, and everyone else meets the fail-closed refusal.
    """
    import config

    with config.override(RATE_ROOMS_PER_DAY=3):
        for i in range(3):
            assert client.get(f"/r/fresh{i}/say/bot/hi").status_code == 200

        r = client.get("/r/one-too-many/say/bot/hi")
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0  # machine-readable...
        assert "retry after:" in r.text  # ...and in the body, which is all most harnesses
        # show
        assert "room-creation budget spent" in r.text
        # The refusal has to leave the caller something to do *now*, or it is an outage
        # with a timer on it. Rooms that exist are the answer, so the reply has to say so.
        assert "ALREADY EXISTS" in r.text and "/r/lobby" in r.text
        assert "one-too-many" not in client.get("/rooms").text  # and nothing was created

        # The budget refills rather than resetting: no cliff, no stampede at a window
        # boundary.
        assert "refills continuously" in r.text
        # Rooms this IP already has are untouched — the property that keeps work moving.
        assert client.get("/r/fresh0/say/bot/still%20here").status_code == 200


def test_writing_to_an_existing_room_never_spends_the_room_budget(client, monkeypatch):
    """The budget is on *creation*. A long conversation in one room must cost exactly one."""
    import config

    # 500 writes/min isolates this from the write limit
    with config.override(RATE_ROOMS_PER_DAY=2, RATE_WRITE=500):
        assert client.get("/r/only/say/bot/hi").status_code == 200
        for i in range(40):
            assert client.get(f"/r/only/say/bot/msg{i}").status_code == 200
        assert client.get("/r/second/say/bot/hi").status_code == 200  # the 2nd and last
        assert client.get("/r/third/say/bot/hi").status_code == 429


def test_only_the_request_that_creates_a_room_pays_for_it(client, monkeypatch):
    """The gate charges before the write, so racing first-writers all pay; only one creates.

    Agents converging on a shared rendezvous room is a documented pattern, so a swarm
    behind one NAT could otherwise spend a whole day's budget opening a single room. The
    loser appends to a room that exists by the time it gets through, and is refunded.
    """
    import app as app_module
    import config

    with config.override(RATE_ROOMS_PER_DAY=3):
        # The race, made deterministic: the first two gate checks both see the room as
        # absent, which is exactly what two concurrent first-writers see. Timing alone
        # would reproduce this only sometimes, and a test that passes by accident is worse
        # than none — this one was written the sequential way first and passed with the
        # refund deleted.
        real = app_module._room_exists
        seen = {"n": 0}

        def racing(room: str) -> bool:
            seen["n"] += 1
            return False if seen["n"] <= 2 else real(room)

        monkeypatch.setattr(app_module, "_room_exists", racing)

        for _ in range(3):
            assert client.get("/r/rendezvous/say/bot/hi").status_code == 200

        # One creation happened, so one token is spent: the loser appended to a room that
        # already existed (seq 2) and got its token back. Two of three left = two more
        # rooms.
        assert client.get("/r/second-room/say/bot/hi").status_code == 200
        assert client.get("/r/third-room/say/bot/hi").status_code == 200
        assert client.get("/r/fourth-room/say/bot/hi").status_code == 429


def test_the_post_lanes_do_not_block_the_event_loop(client, monkeypatch):
    """A POST must not stall every *other* request while it touches disk.

    room_post and note_post are `async def` — they have to await the request body — so any
    blocking store call they make runs on the event loop rather than in the threadpool
    Starlette gives a sync endpoint for free. That is not theoretical: against a full store
    one POST made every other in-flight request wait ~385 ms, measured with a /healthz
    probe (tests/capacity_bench.py reproduces it).

    Rather than build a full store, this makes one store call slow and asks whether an
    unrelated route can still be served while it runs. /healthz touches no disk, so any
    latency it sees here is the loop being unavailable.
    """
    import asyncio
    import time

    from httpx2 import ASGITransport, AsyncClient

    import app as app_module

    def slowed(fn):
        def wrapper(*args, **kwargs):
            time.sleep(0.5)  # stands in for flock + fsync + a reap pass
            return fn(*args, **kwargs)

        return wrapper

    # Both async lanes, because they are the same mistake in two places and a fix applied
    # to only one of them should still fail this.
    monkeypatch.setattr(app_module.store, "append", slowed(app_module.store.append))
    monkeypatch.setattr(app_module.store, "note_set", slowed(app_module.store.note_set))

    # A heartbeat, not a single timed request. Timing one request racing the POST measures
    # nothing: a blocking call stalls the loop's *timers* too, so the sleep meant to line
    # the two up only resumes once the block is over and the request that follows it is
    # served promptly. Sampling continuously is what shows the gap.
    async def race() -> tuple[float, int]:
        gaps: list[float] = []
        done = asyncio.Event()

        async def heartbeat() -> None:
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.03)  # let it tick once before the request, so gaps is seeded
        async with AsyncClient(
            transport=ASGITransport(app=app_module.app), base_url="http://t"
        ) as c:
            posted = await c.post("/r/lobby", json={"from": "bot", "text": "hi"})
            noted = await c.post("/kv/ns/k", json={"value": "v"})
        done.set()
        await beat
        # Empty means the loop never ran the heartbeat *once* across both requests, which is
        # the most blocked it can possibly be — not a reason to raise ValueError from max().
        return (max(gaps) if gaps else float("inf")), min(posted.status_code, noted.status_code)

    stall, status = asyncio.run(race())
    assert status == 200
    # The POST itself takes 0.5s either way. The question is only whether the loop was
    # available during it, so the threshold sits far below that and far above a scheduling
    # hiccup: blocked measures ~0.5s, threadpooled ~0.01s.
    assert stall < 0.2, (
        f"the event loop went unserved for {stall * 1000:.0f} ms during one POST — its "
        "store call is running on the loop instead of in run_in_threadpool"
    )


def test_junk_query_params_never_500(client):
    """A harness that mangles a URL must get the default view, not a stack trace."""
    client.get("/r/lobby/say/bot/hi")
    for q in ("since=²", "limit=²", "since=abc", "limit=-3", "since=", "limit=1e3"):
        r = client.get(f"/r/lobby?{q}")
        assert r.status_code == 200, q
        assert "hi" in r.text, q


def test_rate_limit_buckets_are_bounded(client, monkeypatch):
    """Every unseen IP adds entries; unbounded, a rotating-IP flood OOMs the container."""
    import app as app_module
    import config

    monkeypatch.setattr(app_module, "MAX_BUCKETS", 8)
    # Opted in explicitly: no forwarded header is trusted by default, so without this the
    # whole loop is one client (the test socket) and nothing rotates.
    with config.override(CLIENT_IP_HEADER="cf-connecting-ip"):
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
    import config

    with config.override(CLIENT_IP_HEADER="cf-connecting-ip"):
        client.get("/r/lobby", headers=spoofed)
        assert ("203.0.113.9", "read") in app_module._buckets


def test_an_empty_trusted_proxy_header_falls_back_to_the_socket_peer(client, monkeypatch):
    """A missing/blank edge header must not collapse callers into an empty-string bucket.

    This also refuses the tempting but unsafe fallback to a later comma-separated value:
    the configured proxy owns the first hop, while anything after it may be caller input.
    """
    import app as app_module
    import config

    with config.override(CLIENT_IP_HEADER="cf-connecting-ip"):
        app_module._buckets.clear()
        client.get("/r/lobby", headers={"cf-connecting-ip": " , 198.51.100.7"})

        identities = {ip for ip, kind in app_module._buckets if kind == "read"}
        assert identities == {"testclient"}
        assert "" not in identities and "198.51.100.7" not in identities


def _dockerfile_cmd() -> list[str]:
    """The argv the shipped image actually runs, out of the CMD JSON array."""
    raw = (Path(__file__).resolve().parents[2] / "docker" / "Dockerfile").read_text()
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


def test_a_budget_warning_never_reaches_the_json_lane(client, monkeypatch):
    """`respond` appends the budget note to the plain-text branch only, and the page's
    post_message and read_room tools parse the JSON one. A note glued onto JSON would not
    degrade, it would stop parsing — and only once a caller was near its limit, which is
    the worst moment to discover it. Pinned because the number of `note=` callers grew.
    """
    import config

    with config.override(RATE_WRITE=8):
        for _ in range(6):
            client.post("/r/lobby", json={"from": "a", "text": "x"})

        posted = client.post("/r/lobby?format=json", json={"from": "a", "text": "final"})
        assert posted.headers["content-type"].startswith("application/json")
        assert posted.json()["posted"]["text"] == "final"
        assert "# budget:" not in posted.text

        # The warning is not lost, it belongs to the lane that can carry it.
        assert "# budget:" in client.post("/r/lobby", json={"from": "a", "text": "y"}).text


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


def test_oversize_body_is_refused_before_it_is_buffered(client):
    """`await request.body()` buffers everything first, so the size check has to come
    from Content-Length (and a streaming cap for chunked uploads)."""
    import app as app_module

    r = client.post("/r/lobby", content=b"x" * (app_module.MAX_BODY + 1))
    assert r.status_code == 413 and "too large" in r.text
    assert "no rooms yet" in client.get("/rooms").text  # nothing was written


def test_chunked_body_is_stopped_at_the_same_cap_and_says_how_to_split_it(client):
    """Content-Length is optional, so the streaming path is the actual memory-safety bound
    against a chunked upload. Its body must also give a usable correction without headers.
    """
    import asyncio

    from starlette.requests import Request
    from starlette.responses import Response

    import app as app_module

    chunks = iter((b"x" * app_module.MAX_BODY, b"y"))

    async def receive():
        chunk = next(chunks)
        return {"type": "http.request", "body": chunk, "more_body": chunk.endswith(b"x")}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/r/lobby",
            "headers": [],
            "client": ("203.0.113.8", 1234),
        },
        receive,
    )
    response = asyncio.run(app_module.read_json(request))
    assert isinstance(response, Response)
    assert response.status_code == 413
    body = bytes(response.body).decode()
    assert "the stream passed it before it ended" in body
    assert "multiple room lines" in body and "multiple keys" in body


def test_malformed_payload_shapes_are_400_not_500(client):
    for body in ("[1,2,3]", '"a string"', "42", "null", "true"):
        r = client.post(
            "/r/lobby", content=body.encode(), headers={"content-type": "application/json"}
        )
        assert r.status_code == 400, body
    assert client.post("/r/lobby", content=b"{not json").status_code == 400
    assert client.post("/r/lobby", json={}).status_code == 400  # empty from/text


def test_a_malformed_note_post_names_the_note_shape_to_send_next(client):
    """The shared body parser mentions both POST envelopes. Exercise it through the note
    route too so future room-focused wording cannot leave note clients without a correction.
    """
    response = client.post("/kv/plans/next", content=b"value=ship")
    assert response.status_code == 400
    assert '{"value":"..."}' in response.text
    assert "body must be JSON" in response.text


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
    """The documented cap is in *characters*. json.dumps defaults to ensure_ascii=True,
    so astral characters cost 12 body bytes each as surrogate-pair escapes. The byte cap
    must not silently shrink the character limit for clients using that default."""
    import json

    import store

    for label, ch in (("ascii", "a"), ("cjk", "日"), ("emoji", "\U0001f600")):
        text_value = ch * store.MAX_TEXT_CHARS
        escaped = json.dumps({"from": "agent", "text": text_value}, ensure_ascii=True)
        r = client.post(
            f"/r/enc-{label}",
            content=escaped.encode(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 200, (label, len(escaped), r.text[:200])
        stored = client.get(f"/r/enc-{label}?format=json").json()["messages"][0]["text"]
        assert len(stored) == store.MAX_TEXT_CHARS and stored == text_value


def test_body_cap_still_refuses_oversize_uploads(client):
    import app as app_module

    over = b"x" * (app_module.MAX_BODY + 1000)
    assert client.post("/r/lobby", content=over).status_code == 413
    # declared-but-not-sent is refused on Content-Length alone, before buffering
    assert (
        client.post(
            "/r/lobby", content=b"x" * 100, headers={"content-length": "99999999"}
        ).status_code
        == 413
    )


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


def test_long_poll_surfaces_a_message_that_arrives_after_the_request(client, monkeypatch):
    """This is the behavior long-polling exists for; a timeout-only test never exercises
    the wake-up path and would miss a refactor that silently turned every wait into polling.
    """
    from concurrent.futures import ThreadPoolExecutor

    import app as app_module

    client.get("/r/lobby/say/bot/first")
    monkeypatch.setattr(app_module, "WAIT_POLL", 0.01)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(client.get, "/r/lobby?since=1&wait=2&format=json")
        deadline = time.monotonic() + 1
        while app_module._waiters_total == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert app_module._waiters_total == 1, "the read never acquired a bounded waiter slot"
        client.get("/r/lobby/say/bot/second")
        response = waiting.result(timeout=2)

    assert [message["text"] for message in response.json()["messages"]] == ["second"]
    assert app_module._waiters_total == 0 and app_module._waiters_by_ip == {}


def test_long_poll_refuses_excess_slots_immediately_and_releases_disconnects(client, monkeypatch):
    """Both exits are resource-safety paths: an attacker gets no unbounded parked sockets,
    and a caller that vanished stops causing tail reads before its timeout expires.
    """
    import asyncio
    from types import SimpleNamespace
    from typing import cast

    from starlette.requests import Request

    import app as app_module

    client.get("/r/lobby/say/bot/first")
    monkeypatch.setattr(app_module, "MAX_WAITERS_TOTAL", 0)
    started = time.monotonic()
    refused = client.get("/r/lobby?since=1&wait=10&format=json")
    assert time.monotonic() - started < 1
    assert refused.json()["messages"] == []

    monkeypatch.setattr(app_module, "MAX_WAITERS_TOTAL", 64)
    monkeypatch.setattr(app_module, "WAIT_POLL", 0)

    class Gone:
        headers = {}
        client = SimpleNamespace(host="203.0.113.7")

        async def is_disconnected(self):
            return True

    result = asyncio.run(app_module._await_messages(cast(Request, Gone()), "lobby", 50, 1, 10))
    assert result is None
    assert app_module._waiters_total == 0 and app_module._waiters_by_ip == {}


def test_timestamps_carry_microseconds_and_seq_stays_authoritative(client):
    for _ in range(3):
        client.get("/r/lobby/say/bot/burst")
    msgs = client.get("/r/lobby?format=json").json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2, 3]  # contiguous total order
    for m in msgs:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", m["ts"]), m["ts"]


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
    # The pointer has to be fetchable as printed: routes are case-sensitive, so a body
    # that shouted /LLMS.TXT would send an agent that copied it to a 404.
    assert "/llms.txt" in r.text and client.get("/llms.txt").status_code == 200


def test_a_405_carries_allow_and_names_every_verb_the_path_takes(client):
    """RFC 9110 §15.5.6 makes `Allow` mandatory, and the union matters: two routes share
    `/r/<room>` and two share `/kv/<ns>/<key>`, so Starlette's first-partial-match header
    would name `GET, HEAD` on paths that plainly also take POST."""
    for path in ("/r/lobby", "/kv/plans/next"):
        r = client.request("PUT", path)
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD, POST", path
        # Repeated in the body for the same reason Retry-After is: agent harnesses show
        # the body and drop the headers.
        assert "this path accepts: GET, HEAD, POST" in r.text, path

    # A read-only path says so rather than over-promising the POST the neighbours take.
    for path in ("/rooms", "/llms.txt"):
        r = client.request("PATCH", path)
        assert r.status_code == 405 and r.headers["allow"] == "GET, HEAD", path

    # GET-shaped mutations must not inherit Starlette's automatic HEAD: a link probe
    # cannot perform a write while throwing the only useful response body away. All four
    # write lanes say the same thing — the signed two match on shape, so the credentials
    # here are placeholders: the verb is refused before any endpoint reads them.
    for path in (
        "/r/lobby/say/bot/hi",
        "/r/lobby/say-signed/did/sig/1/hi",
        "/kv/plans/next/set/x",
        "/kv/plans/next/set-signed/did/sig/1/x",
    ):
        r = client.request("PATCH", path)
        assert r.status_code == 405 and r.headers["allow"] == "GET", path

    # OPTIONS is not implemented either, so it must not appear in a list of what is.
    options = client.request("OPTIONS", "/healthz")
    assert options.status_code == 405 and "OPTIONS" not in options.headers["allow"]


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
    # Both branches keep the store's own diagnostic ahead of the generic advice: the
    # template says what to do, the exception says which condition actually fired.
    assert absent.text.startswith("409 note plans/absent")
    assert lost.text.startswith("409 note plans/next changed since you read it")


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


def test_the_refill_rate_stays_a_number_an_agent_can_pace_against(client):
    """`{per_min/60:.1f} tokens/s` prints a flat "0.0 tokens/s" below 30/min — useless on
    exactly the deployments that throttle hardest. Under 1/s the period is the useful form.
    """
    import app as app_module

    assert app_module.refill_rate(120) == "2.0 tokens/s"
    assert app_module.refill_rate(60) == "1.0 tokens/s"
    assert app_module.refill_rate(30) == "one token every 2s"
    assert app_module.refill_rate(1) == "one token every 60s"


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
