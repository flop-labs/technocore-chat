"""HTTP capacity figures only describe requests that actually completed successfully."""

from __future__ import annotations

import importlib.util
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "capacity_http_bench", Path(__file__).parents[1] / "capacity_bench.py"
)
assert SPEC and SPEC.loader
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)
pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="HTTP benchmark uses curl")


@pytest.fixture
def origin():
    statuses = {}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def answer(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.path, body))
            self.send_response(statuses.get((self.command, self.path), 200))
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        do_GET = do_POST = answer  # noqa: N815

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, statuses, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status", [302, 400, 429, 503])
def test_refused_or_redirected_reads_are_not_latency_samples(origin, status):
    port, statuses, _ = origin
    statuses["GET", "/rooms"] = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        bench._get(f"http://127.0.0.1:{port}/rooms")


def test_closed_port_does_not_report_success(capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        # Keep ownership of this non-listening port so another process cannot claim it.
        with pytest.raises(subprocess.CalledProcessError):
            bench.http_bench(port, "r0")
    assert "GET /rooms cold" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/r/r0/say/b/hi"),
        ("GET", "/rooms"),
        ("GET", "/healthz"),
        ("GET", "/r/r0/say/b/x"),
        ("POST", "/r/r0"),
    ],
)
def test_every_http_phase_refuses_failed_requests(origin, method, path):
    port, statuses, _ = origin
    statuses[method, path] = 503
    before = set(threading.enumerate())
    with pytest.raises(RuntimeError, match="HTTP 503"):
        bench.http_bench(port, "r0")
    # The sampler must have been joined even when the measured write raises.
    assert not [t for t in threading.enumerate() if t not in before and not t.daemon]


def test_success_keeps_get_and_post_measurements(origin, capsys):
    port, _, requests = origin
    bench.http_bench(port, "r0")
    output = capsys.readouterr().out
    assert "GET /rooms cold" in output
    assert "POST /r/<room> (async)" in output
    assert ("POST", "/r/r0", b'{"from":"b","text":"x"}') in requests


def test_curl_has_a_total_deadline_and_propagates_timeout(monkeypatch):
    def timeout(args, **kwargs):
        assert args[args.index("--max-time") + 1] == "30"
        assert kwargs["check"] is True
        assert not kwargs.get("shell")
        raise subprocess.CalledProcessError(28, args)

    monkeypatch.setattr(bench.subprocess, "run", timeout)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        bench._get("http://127.0.0.1:1/rooms")
    assert exc.value.returncode == 28


def test_delayed_sampler_fails_explicitly_and_is_joined(monkeypatch):
    release = threading.Event()
    before = set(threading.enumerate())

    class DelayedSampler(threading.Thread):
        def run(self):
            release.wait()
            super().run()

        def join(self, timeout=None):
            release.set()
            super().join(timeout)

    # Even the real sleeps cannot guarantee the sampler gets a turn before stop is set.
    monkeypatch.setattr(threading, "Thread", DelayedSampler)
    monkeypatch.setattr(bench, "_curl", lambda *args: None)
    monkeypatch.setattr(bench, "_get", lambda url: pytest.fail("sampler should already be stopped"))
    with pytest.raises(RuntimeError, match="no latency samples"):
        bench._loop_stall("http://unused", ("http://unused/write",))

    assert release.is_set()
    assert not [t for t in threading.enumerate() if t not in before and not t.daemon]
