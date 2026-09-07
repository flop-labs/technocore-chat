"""Run: uv run --group dev python -m pytest tests"""

import json
import os
from pathlib import Path

import _client
import httpx2 as httpx  # the declared dependency; starlette.testclient aliases it the same way
import pytest
from starlette.testclient import TestClient

client = _client.client  # the shared TestClient fixture


def test_a_graceful_shutdown_flushes_the_batched_counters(client):
    """A rolling deploy is a SIGTERM, not a kill.

    A plain message bump rides in memory until something structural, the message bound or a
    snapshot flushes it (#588) — so without a shutdown hook every ordinary restart would drop
    what each worker was still holding, which is a much larger and much more frequent loss
    than the hard-kill window these counters actually document. `TestClient` runs the lifespan
    only as a context manager, and that is the same startup/shutdown pair uvicorn drives.
    """
    import config
    import store

    client.get("/r/lobby/say/bot/one")  # creates the room: structural, so it lands at once
    client.get("/r/lobby/say/bot/two")
    assert store._PENDING[config.ROOT] == {"messages": 1}, "the second message should be riding"

    with client:  # enter and leave: the ASGI lifespan, startup through shutdown
        pass

    assert store.counters(config.ROOT)["messages"] == 2, "the shutdown dropped the batch"
    assert config.ROOT not in store._PENDING


def test_stats_says_whether_per_ip_limits_are_actually_per_ip(client, monkeypatch):
    """Behind a CDN with no CHAT_CLIENT_IP_HEADER every caller shares one bucket, and the
    per-day room budget then bounds the whole world at once. Silent, and indistinguishable
    from an outage — so the evidence is published rather than left to be guessed at."""
    import config

    with config.override(STATS_TOKEN="t", STATS_CACHE_SECONDS=0):
        for i in range(3):
            client.get("/r/lobby", headers={"CF-Connecting-IP": f"203.0.113.{i}"})
        ident = client.get("/stats", headers={"X-Stats-Token": "t"}).json()["client_identity"]
        assert ident["client_ip_header"] is None
        assert ident["proxied_requests_ignored"] >= 3  # three real callers...
        assert ident["distinct_identities"] == 1  # ...seen as one


@pytest.fixture()
def stats_client(tmp_path, monkeypatch):
    """A client whose service has the stats token configured (the deployed shape)."""
    import app as app_module
    import config

    # Test bodies read CHAT_ROOT back for direct store access; the knob itself comes from
    # config.override now, not the environment.
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    app_module._buckets.clear()
    app_module._rooms_walk.cache_clear()
    app_module._identities.clear()
    app_module._proxy_evidence["proxied_requests"] = 0
    with config.override(  # every stats call recomputes, so a test can observe its writes
        ROOT=tmp_path, STATS_TOKEN="s3cret", STATS_CACHE_SECONDS=0, DUPE_FILTER_SECONDS=0
    ):
        yield TestClient(app_module.app)


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


def _token_bytes(raw: bytes) -> httpx.Headers:
    """`X-Stats-Token` as bytes, through the client stack. `headers=` is typed `Mapping[str, str]`
    and httpx encodes a str value as ASCII, so neither can carry a byte above 0x7F — which is
    why no existing test ever reached the compare. `httpx.Headers` built from byte pairs is a
    Mapping for the checker; on the wire it keeps valid UTF-8 as sent but re-encodes a lone
    high byte (0xF6 arrives as C3 B6). Either way the handler sees non-ASCII latin-1 text, which
    is what raised. The exact malformed-byte round trip is pinned by the raw-ASGI test below."""
    return httpx.Headers([(b"x-stats-token", raw)])


def test_a_non_ascii_token_gets_the_same_404_as_an_unrouted_path(stats_client):
    """`secrets.compare_digest` refuses non-ASCII *strings* with a TypeError, and Starlette
    hands the handler the header as latin-1 text, so any byte above 0x7F in `X-Stats-Token`
    raised — a 500. That is the one answer that tells a prober the route exists: a path
    that was never routed does not 500 on a header. The 404 must stay byte-identical for
    a high byte exactly as it does for a wrong ASCII token, and the right token must still
    open the door."""
    missing = stats_client.get("/definitely-not-a-route")
    for raw in (b"t\xf6ken", b"\xe2\x9c\x93", b"\xff", b"s3cret\xc3\xa9"):
        probe = stats_client.get("/stats", headers=_token_bytes(raw))
        assert probe.status_code == missing.status_code, raw
        assert probe.text == missing.text, raw
    assert stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).status_code == 200


def _raw_asgi_get(app, path: str, token: bytes | None) -> tuple[int, bytes]:
    """One GET straight into the ASGI app with the header bytes EXACTLY as given — no client
    stack in between to re-encode them. This is the only way to put a lone high byte on the
    wire, and a lone high byte is the malformed case the latin-1 round trip exists for."""
    import asyncio

    headers = [(b"host", b"t")] + ([(b"x-stats-token", token)] if token is not None else [])
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("t", 80),
        "client": ("127.0.0.1", 1),
        "headers": headers,
    }
    out: dict = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


def test_a_lone_high_byte_reaches_the_compare_as_latin_1_and_still_404s(stats_client):
    """The client-stack tests above send bytes that arrive as valid UTF-8. A *malformed* header
    — a single byte above 0x7F with no UTF-8 sequence around it — is the case the latin-1
    round trip is for, and no client will send one, so it goes straight into the ASGI app.
    Starlette decodes it as latin-1 (0xF6 -> U+00F6); encoding it back is the same byte; the
    compare then runs on bytes and returns the byte-identical 404, where it used to raise."""
    app = stats_client.app
    missing_status, missing_body = _raw_asgi_get(app, "/definitely-not-a-route", None)
    for raw in (bytes.fromhex("f6"), bytes.fromhex("ff"), b"s3cret" + bytes.fromhex("f6")):
        status, body = _raw_asgi_get(app, "/stats", raw)
        assert status == missing_status == 404, raw
        assert body == missing_body, raw
    assert _raw_asgi_get(app, "/stats", b"s3cret")[0] == 200


def test_a_non_ascii_configured_token_can_be_presented(tmp_path, monkeypatch):
    """The same TypeError fired on the *configured* side: a token an operator set to
    non-ASCII made the endpoint a 500 for every caller, the right one included, because the
    string compare could never run. Both sides are bytes now — UTF-8 on the wire, its
    latin-1 round-trip in the header — so the operator's token is simply a token."""
    import app as app_module
    import config

    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    app_module._buckets.clear()
    app_module._rooms_walk.cache_clear()
    with config.override(ROOT=tmp_path, STATS_TOKEN="t\u00f6k\u00e9n", STATS_CACHE_SECONDS=0):
        client = TestClient(app_module.app)
        right = "t\u00f6k\u00e9n".encode("utf-8")
        assert client.get("/stats", headers=_token_bytes(right)).status_code == 200
        assert client.get("/stats", headers=_token_bytes(b"t\xc3\xb6k\xc3\xa9x")).status_code == 404
        assert client.get("/stats", headers={"X-Stats-Token": "wrong"}).status_code == 404


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
    assert rooms["listed"] == 5 and rooms["capacity"] == store.MAX_ROOMS
    assert view["notes"]["total"] == 1 and view["bytes"]["rooms"] > 0

    for secret in ("verysecret", "privatens", "somekey", "somenick", "postbox", "openroom"):
        assert secret not in body


def test_stats_reports_traffic_against_uptime(stats_client):
    """Request counters are only readable as a rate, so they ship with the uptime."""
    stats_client.get("/rooms")
    stats_client.get("/r/lobby/say/bot/hi")
    view = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).json()
    assert view["requests"]["read"] >= 1 and view["requests"]["write"] >= 1
    assert view["requests"]["uptime_seconds"] >= 0
    assert view["capacity_limits"]["read_per_min"] == 120


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


def test_stats_cache_avoids_repeating_the_expensive_store_walk(stats_client, monkeypatch):
    """The token is not a cost bound: a leaked token can be replayed, so the O(capacity)
    stats walk still needs the short cache promised by the handler.
    """
    import app as app_module
    import config

    real_view = app_module._stats_view
    calls = []

    def counted():
        calls.append(1)
        return real_view()

    with config.override(STATS_CACHE_SECONDS=60):
        monkeypatch.setattr(app_module, "_stats_view", counted)
        app_module._stats_cache = (0.0, {})
        headers = {"X-Stats-Token": "s3cret"}
        first = stats_client.get("/stats", headers=headers)
        second = stats_client.get("/stats", headers=headers)
        assert first.status_code == second.status_code == 200
        assert calls == [1]
