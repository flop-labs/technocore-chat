"""Run the Worker's real transport through MCP and an origin that redirects.

CPython cannot import the Cloudflare runtime. Only its platform fetch is substituted:
httpx follows the HTTP redirect policy, while worker.py, the MCP SDK and the service
all execute unchanged. This tests the adapter's policy, not Pyodide's FFI or deployment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx2
import pytest
import test_mcp
from starlette.responses import RedirectResponse

import app

mcp = test_mcp.mcp
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def worker_origin(mcp, monkeypatch):
    reached = []
    status = [301]
    committed = [False]

    async def origin(scope, receive, send):
        if scope["path"].startswith("/moved/"):
            if committed[0] and scope["method"] == "POST":
                # A legitimate post/redirect/get origin can store before redirecting.
                # Discard its first response, then send the redirect on the wire.
                async def discard(message):
                    pass

                await app.app(
                    {**scope, "path": scope["path"].removeprefix("/moved")}, receive, discard
                )
            response = RedirectResponse(scope["path"].removeprefix("/moved"), status[0])
            await response(scope, receive, send)
        else:
            reached.append((scope["method"], scope["path"]))
            await app.app(scope, receive, send)

    async def platform_fetch(url, *, method, headers, body=None, redirect="follow"):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=origin), follow_redirects=redirect == "follow"
        ) as client:
            response = await client.request(method, url, headers=headers, content=body)

        async def text():
            return response.text

        return SimpleNamespace(status=response.status_code, text=text)

    monkeypatch.setitem(
        sys.modules,
        "workers",
        SimpleNamespace(Response=None, WorkerEntrypoint=object, asgi=None, fetch=platform_fetch),
    )
    spec = importlib.util.spec_from_file_location(
        "mcp_worker_test", ROOT / "mcp" / "worker" / "src" / "worker.py"
    )
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    monkeypatch.setattr(mcp.module, "_fetch", worker.workers_fetch)
    monkeypatch.setattr(mcp.module, "BASE_URL", "http://worker.test/moved")
    return SimpleNamespace(reached=reached, status=status, committed=committed, worker=worker)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("say", {"room": "lobby", "text": "привет"}),
        ("write_note", {"namespace": "notes", "key": "one", "value": "привет"}),
        ("say_signed", {"room": "mb-inbox", "text": "привет"}),
        ("claim_room", {"room": "d-desk"}),
        ("set_room_allow", {"room": "d-desk", "dids": ""}),
    ],
)
def test_worker_write_redirect_never_reaches_destination(
    mcp, monkeypatch, worker_origin, status, tool, arguments
):
    did = test_mcp.with_key(mcp, monkeypatch)
    if tool == "set_room_allow":
        monkeypatch.setattr(mcp.module, "BASE_URL", "http://worker.test")
        claimed = mcp.call("claim_room", {"room": "d-desk"})
        assert claimed.is_error is False, test_mcp.text_of(claimed)
        monkeypatch.setattr(mcp.module, "BASE_URL", "http://worker.test/moved")
        worker_origin.reached.clear()
        arguments = {**arguments, "dids": did}
    worker_origin.status[0] = status
    reply = mcp.call(tool, arguments)
    assert reply.is_error is True, test_mcp.text_of(reply)
    assert f"HTTP {status}" in test_mcp.text_of(reply)
    assert "TECHNOCORE_URL" in test_mcp.text_of(reply)
    assert worker_origin.reached == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_worker_read_redirect_is_still_followed(mcp, worker_origin, status):
    worker_origin.status[0] = status
    reply = mcp.call("read_room", {"room": "lobby"})
    assert reply.is_error is False, test_mcp.text_of(reply)
    assert worker_origin.reached == [("GET", "/r/lobby")]


def test_worker_nonredirected_write_lands_with_its_original_body(mcp, monkeypatch, worker_origin):
    monkeypatch.setattr(mcp.module, "BASE_URL", "http://worker.test")
    reply = mcp.call("say", {"room": "lobby", "text": "привет", "nick": "probe"})
    assert reply.is_error is False, test_mcp.text_of(reply)
    found = mcp.call("read_room", {"room": "lobby"})
    assert "привет" in test_mcp.text_of(found)
    assert worker_origin.reached == [("POST", "/r/lobby"), ("GET", "/r/lobby")]


def test_worker_commit_then_redirect_does_not_claim_the_write_was_lost(mcp, worker_origin):
    worker_origin.status[0] = 303
    worker_origin.committed[0] = True
    reply = mcp.call("say", {"room": "lobby", "text": "stored once"})
    assert reply.is_error is True
    message = test_mcp.text_of(reply)
    assert "this write was not confirmed" in message
    assert "check whether it landed before retrying" in message
    assert worker_origin.reached == []
    found = test_mcp.text_of(mcp.call("read_room", {"room": "lobby"}))
    assert found.count("stored once") == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_worker_bodiless_write_does_not_follow_a_redirect(worker_origin, method):
    # Classify by method rather than body presence, just like the stdlib transport.
    status, _ = anyio.run(
        worker_origin.worker.workers_fetch,
        method,
        "http://worker.test/moved/r/lobby",
        {},
        None,
        5.0,
    )
    assert status == 301
    assert worker_origin.reached == []


def test_worker_head_redirect_is_still_followed(worker_origin):
    status, body = anyio.run(
        worker_origin.worker.workers_fetch,
        "HEAD",
        "http://worker.test/moved/r/lobby",
        {},
        None,
        5.0,
    )
    assert status == 200
    assert body == ""
    assert worker_origin.reached == [("HEAD", "/r/lobby")]
