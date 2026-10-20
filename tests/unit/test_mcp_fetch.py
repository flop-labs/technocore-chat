"""The one function in the wrapper that actually touches a socket.

`tests/test_mcp.py` replaces this seam wholesale — that is what the seam is for, and it
is how the tools get driven against the real service without a network. Which leaves the
stdlib implementation itself, the code every `uvx technocore-mcp` user runs and no other
test reaches, so it is exercised here against a real listening socket on loopback.

Three things have to hold, and only the first is obvious:

* a 200 comes back as `(200, body)`;
* so does a 404 or a 429 — a failure *with* an HTTP answer is a value here, never an
  exception, because the service puts the actionable part of every refusal in the body
  and `urllib` would otherwise raise it away;
* a failure with no answer at all raises `OSError`, which is the one thing the layer
  above catches to say "cannot reach".
"""

from __future__ import annotations

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anyio
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp" / "src"))


class _Handler(BaseHTTPRequestHandler):
    """Echoes the method, path, User-Agent and any body; takes its status from the path."""

    def _answer(self):
        status = 429 if self.path.startswith("/slow-down") else 200
        length = int(self.headers.get("Content-Length") or 0)
        received = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        body = (
            f"{status} for {self.command} {self.path} "
            f"ua={self.headers.get('User-Agent')} body={received}"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer  # noqa: N815 - BaseHTTPRequestHandler's own spelling

    def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
        pass  # a test server logging every request to stderr is noise, not evidence


@pytest.fixture()
def origin():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(url: str, headers: dict[str, str] | None = None, *, method="GET", body=None):
    from technocore_mcp.fetch import urllib_fetch

    return anyio.run(urllib_fetch, method, url, headers or {}, body, 5.0)


def test_a_success_comes_back_as_its_status_and_body(origin):
    status, body = fetch(f"{origin}/r/lobby", {"User-Agent": "technocore-mcp/test"})
    assert status == 200
    assert "for GET /r/lobby" in body
    assert "ua=technocore-mcp/test" in body


def test_a_post_carries_its_body_bytes_verbatim(origin):
    """The write lanes ride this: the body arrives already encoded, and the seam's whole
    job is to move it unmodified."""
    status, body = fetch(
        f"{origin}/kv/ns/key",
        {"Content-Type": "application/json"},
        method="POST",
        body='{"value": "привет"}'.encode(),
    )
    assert status == 200
    assert "for POST /kv/ns/key" in body
    assert 'body={"value": "привет"}' in body


def test_an_http_failure_comes_back_as_a_value_with_its_body_intact(origin):
    """`urllib` raises `HTTPError` for any 4xx/5xx — and an `HTTPError` is also the
    response, so the body is still there to read. Reading it here is what lets a 429's
    retry advice reach the model instead of the words "HTTP Error 429"."""
    status, body = fetch(f"{origin}/slow-down")
    assert status == 429
    assert "for GET /slow-down" in body


def test_no_answer_at_all_raises_oserror():
    """A refused connection, which is what a misconfigured TECHNOCORE_URL looks like."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    with pytest.raises(OSError):
        fetch(f"http://127.0.0.1:{closed}/r/lobby")
