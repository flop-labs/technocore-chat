"""Integration tests validating examples/agent_poller.py against app routes."""

from __future__ import annotations

import email.message
import io
import sys
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import app as app_module  # noqa: E402
from examples.agent_poller import AgentClient  # noqa: E402


@runtime_checkable
class _Readable(Protocol):
    def read(self) -> bytes: ...


class _MockAddInfoUrl(urllib.response.addinfourl):
    """Subclass of addinfourl exposing typed headers and msg attributes."""

    def __init__(
        self,
        fp: io.BytesIO,
        headers: email.message.Message,
        url: str,
        code: int,
    ) -> None:
        super().__init__(fp, headers, url, code)
        self.msg = headers


class StarletteHTTPHandler(urllib.request.HTTPHandler):
    """Routes standard library urllib requests into in-memory Starlette TestClient."""

    def __init__(self, client: TestClient) -> None:
        super().__init__()
        self._client = client

    def http_open(self, req: urllib.request.Request) -> Any:
        url = req.full_url
        path = "/" + url.split("/", 3)[-1] if url.count("/") >= 3 else "/"
        method = req.get_method()
        headers = dict(req.headers)

        body: bytes | None = None
        data = req.data
        if isinstance(data, bytes):
            body = data
        elif isinstance(data, _Readable):
            body = data.read()

        resp = self._client.request(method=method, url=path, headers=headers, content=body)

        msg = email.message.Message()
        for k, v in resp.headers.items():
            msg[k] = v

        fp = io.BytesIO(resp.content)
        res = _MockAddInfoUrl(fp, msg, req.full_url, resp.status_code)

        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                url=req.full_url,
                code=resp.status_code,
                msg=resp.reason_phrase or "Error",
                hdrs=msg,  # type: ignore[arg-type]
                fp=fp,
            )
        return res


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    monkeypatch.setattr(app_module, "ROOT", tmp_path)
    return TestClient(app_module.app)


def test_agent_client_lifecycle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = StarletteHTTPHandler(client)
    opener = urllib.request.build_opener(handler)
    monkeypatch.setattr(urllib.request, "urlopen", opener.open)

    agent = AgentClient(base_url="http://testserver")

    # 1. Post via signed GET
    ok_get = agent.say_signed_get("lobby", "hello from signed get")
    assert ok_get is True

    # 2. Post via signed POST
    ok_post = agent.say_signed_post("lobby", "hello from signed post")
    assert ok_post is True

    # 3. Read room and verify messages
    view = agent.read_room("lobby", wait=0)
    assert view is not None
    assert view["count"] == 2
    texts = [m["text"] for m in view["messages"]]
    assert "hello from signed get" in texts
    assert "hello from signed post" in texts

    # 4. Monotonic cursor polling
    last_seq = view["last_seq"]
    agent.say_signed_post("lobby", "new message")
    incremental_view = agent.read_room("lobby", since=last_seq, wait=0)
    assert incremental_view is not None
    assert len(incremental_view["messages"]) == 1
    assert incremental_view["messages"][0]["text"] == "new message"
