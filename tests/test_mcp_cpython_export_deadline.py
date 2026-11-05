"""Exercise export deadlines over real sockets, including continuously progressing peers."""

import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import anyio
import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp" / "src"))
from technocore_mcp import fetch  # noqa: E402


@pytest.fixture
def origin(monkeypatch):
    stop = Event()
    disconnected = Event()
    # Keep first-use HTTP/TLS module loading out of the deliberately short wire deadline.
    # The real client and sockets are still used, and the adapter owns their cleanup.
    client = httpx2.AsyncClient(timeout=5, follow_redirects=True, trust_env=False)
    monkeypatch.setattr(fetch.httpx2, "AsyncClient", lambda **kw: client)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            try:
                if self.path == "/headers":
                    self.wfile.write(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                else:
                    self.send_response(429 if self.path == "/error" else 200)
                    self.send_header("X-Room-Generation", "7")
                    self.end_headers()
                    if self.path == "/partial":
                        self.wfile.write(b'{"seq":1,"text":"')
                # Each gap is smaller than the timeout, but the whole response is not.
                for i in range(40):
                    if stop.wait(0.05):
                        return
                    raw = f'{{"seq":{i + 1}}}\n'.encode() if self.path == "/body" else b"x"
                    self.wfile.write(raw)
                if self.path == "/headers":
                    self.wfile.write(b"\r\n\r\n")
            except OSError:
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", disconnected
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("stage", ["headers", "body", "partial", "error"])
def test_export_deadline_stops_slow_progress_and_closes_connection(origin, stage):
    url, disconnected = origin
    started = time.monotonic()
    with pytest.raises(OSError):
        anyio.run(fetch.http_export_fetch, f"{url}/{stage}", {}, 0.3, None, 200)
    assert time.monotonic() - started < 1.2
    # Returning early while leaving a urllib worker reading is not cancellation.
    assert disconnected.wait(1)


def test_export_caller_cancellation_closes_connection(origin):
    url, disconnected = origin

    async def scenario():
        with anyio.move_on_after(0.3) as scope:
            await fetch.http_export_fetch(f"{url}/body", {}, 10, None, 200)
        assert scope.cancel_called

    anyio.run(scenario)
    assert disconnected.wait(1)


@pytest.mark.parametrize("status", [200, 403, 429, 500])
def test_export_preserves_complete_bodies_and_generation(monkeypatch, status):
    # A split UTF-8 character, CRLF, filtered non-records and an unterminated last line.
    raw = b'not-json\n{}\n{"seq":1}\n{"seq":2,"text":"\xc3\xa9"}\r\n{"seq":3}'
    closed = []

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for byte in raw:
                yield bytes([byte])

        async def aclose(self):
            closed.append(True)

    def respond(request):
        assert request.headers["User-Agent"] == "test-export"
        return httpx2.Response(status, headers={"X-Room-Generation": "7"}, stream=Stream())

    client = httpx2.AsyncClient
    transport = httpx2.MockTransport(respond)
    monkeypatch.setattr(fetch.httpx2, "AsyncClient", lambda **kw: client(transport=transport, **kw))
    code, body, headers = anyio.run(
        fetch.http_export_fetch,
        "https://example.test/export",
        {"User-Agent": "test-export"},
        2,
        1,
        2,
    )
    assert code == status
    expected = raw[raw.index(b'{"seq":2') :]
    assert body == (expected if status == 200 else raw).decode()
    assert headers["x-room-generation"] == "7"
    assert closed == [True]


@pytest.mark.parametrize("error", [httpx2.ConnectError, httpx2.ReadError, httpx2.ReadTimeout])
def test_export_normalizes_transport_errors(monkeypatch, error):
    def fail(request):
        raise error("transport failed")

    client = httpx2.AsyncClient
    transport = httpx2.MockTransport(fail)
    monkeypatch.setattr(fetch.httpx2, "AsyncClient", lambda **kw: client(transport=transport, **kw))
    with pytest.raises(OSError, match="transport failed"):
        anyio.run(fetch.http_export_fetch, "https://example.test/export", {}, 2, None, 2)
