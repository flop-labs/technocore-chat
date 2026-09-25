"""Bounded local regressions for resource consumption, including refused requests."""

import asyncio

import _client
import pytest

import app
import store

client = _client.client


@pytest.mark.parametrize("path", ["/r/slow", "/r/events", "/kv/probe/slow"])
@pytest.mark.parametrize("trickle", [False, True])
def test_upload_has_a_total_deadline(client, monkeypatch, tmp_path, path, trickle):
    monkeypatch.setattr(app, "BODY_TIMEOUT", 0.03, raising=False)

    async def check():
        messages = []

        async def receive():
            # Progress must not reset the deadline; silence must expire as well.
            await asyncio.sleep(0.005 if trickle else 10)
            return {"type": "http.request", "body": b" ", "more_body": True}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 80),
        }
        task = asyncio.create_task(app.app(scope, receive, send))
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if not done:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pytest.fail("an unfinished upload outlived its total body deadline")
        await task
        start = messages[0]
        assert start["status"] == 408
        assert (b"connection", b"close") in start["headers"]
        assert b"body" in messages[-1]["body"]

    asyncio.run(check())
    assert not list(tmp_path.rglob("*.txt"))
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize("lane", ["get", "post"])
def test_missing_note_cas_refusals_create_no_artifacts(client, tmp_path, lane):
    for i in range(12):
        path = f"/kv/probe-{i}/key"
        if lane == "get":
            response = client.get(path + "/set/new", params={"if": "old"})
        else:
            response = client.post(path, json={"value": "new", "if": "old"})
        assert response.status_code == 409
        assert "no note there" in response.text
    assert not (tmp_path / "notes").exists()
    assert store._note_count(tmp_path) == 0
    # Ordinary creation and CAS still work after the rejected requests.
    assert client.post("/kv/probe/key", json={"value": "old"}).status_code == 200
    assert client.post("/kv/probe/key", json={"value": "new", "if": "old"}).status_code == 200


def test_no_request_runs_the_reap_pass(client, tmp_path, monkeypatch):
    """#896's acceptance test, at the HTTP edge: with the throttle due, neither a message nor
    a note write — on either lane — runs the pass inside its own request."""
    ran = []
    monkeypatch.setattr(store, "_reap_pass", lambda root, now: ran.append(now))
    (tmp_path / ".reaped").unlink(missing_ok=True)
    assert client.get("/r/lobby/say/bot/hi").status_code == 200
    assert client.post("/r/lobby", json={"from": "bot", "text": "hi again"}).status_code == 200
    assert client.get("/kv/probe/key/set/v1").status_code == 200
    assert client.post("/kv/probe/key", json={"value": "v2"}).status_code == 200
    assert ran == [], "a request ran the reap pass"


@pytest.mark.parametrize("lane", ["get", "post"])
def test_reaped_note_cas_refusal_recreates_no_artifacts(client, tmp_path, lane):
    endpoint = "/kv/probe/key"
    assert client.post(endpoint, json={"value": "old"}).status_code == 200
    path = store.note_path(tmp_path, "probe", "key")
    lock = path.with_suffix(path.suffix + ".lock")
    _client._age(path, store.IDLE_SECONDS + 60)
    _client._age(lock, store.IDLE_SECONDS + 60)
    # The background pass reaps it between the caller's read and its CAS (#896: the
    # request itself no longer sweeps on the way in).
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    assert not path.exists()
    if lane == "get":
        response = client.get(endpoint + "/set/new", params={"if": "old"})
    else:
        response = client.post(endpoint, json={"value": "new", "if": "old"})
    assert response.status_code == 409
    assert "no note there" in response.text
    assert not path.exists()
    assert not lock.exists()
    assert not store._note_ns_dir(tmp_path, "probe").exists()
    assert store._note_count(tmp_path) == 0
