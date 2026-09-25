import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.unit.technocore_post_test_support import install_trusted_git

TEST_SEED = "0" * 64


def _helper_env(tmp_path, port, repo):
    home = tmp_path / "home"
    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text(TEST_SEED + "\n")
    seed_file.chmod(0o600)

    bin_dir = tmp_path / "git-bin"
    bin_dir.mkdir()
    install_trusted_git(bin_dir, repo)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TECHNOCORE_BASE_URL"] = f"http://127.0.0.1:{port}"
    return home, env


def test_real_helper_serializes_concurrent_delivery(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"

    received = []
    first_entered = threading.Event()
    release_first = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            received.append(payload)

            if len(received) == 1:
                first_entered.set()
                release_first.wait(timeout=5)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, env = _helper_env(tmp_path, server.server_port, repo)

    try:
        a = subprocess.Popen(
            ["bash", str(helper), "test-room", "first"],
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        assert first_entered.wait(timeout=5)

        b = subprocess.Popen(
            ["bash", str(helper), "test-room", "second"],
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(0.2)
        assert b.poll() is None
        release_first.set()

        out_a, err_a = a.communicate(timeout=10)
        out_b, err_b = b.communicate(timeout=10)

        assert a.returncode == 0, err_a
        assert b.returncode == 0, err_b
        assert len(received) == 2

        first_nonce = int(received[0]["nonce"])
        second_nonce = int(received[1]["nonce"])
        assert received[0]["text"] == "first"
        assert received[1]["text"] == "second"
        assert second_nonce > first_nonce
        assert out_a.strip() == "ok"
        assert out_b.strip() == "ok"
    finally:
        release_first.set()
        server.shutdown()
        server.server_close()


def test_real_helper_recovers_from_remote_nonce_high_water(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"

    received = []
    remote_high_water = time.time_ns() // 1_000_000 + 1_000_000

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            received.append(payload)
            nonce = int(payload["nonce"])

            if len(received) == 1:
                assert nonce < remote_high_water
                body = (
                    f"nonce {nonce} is not greater than {remote_high_water}, "
                    "the last one this key used in /r/test-room — a signed URL is "
                    "single-use, so count up"
                ).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            assert nonce > remote_high_water
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home, env = _helper_env(tmp_path, server.server_port, repo)

    try:
        result = subprocess.run(
            ["bash", str(helper), "test-room", "recover"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"
        assert len(received) == 2, "one invocation should recover without walking the gap"

        first_nonce = int(received[0]["nonce"])
        retry_nonce = int(received[1]["nonce"])
        assert first_nonce < remote_high_water < retry_nonce

        nonce_files = list((home / ".config" / "technocore" / "nonces").iterdir())
        assert len(nonce_files) == 1
        assert int(nonce_files[0].read_text().strip()) == retry_nonce
    finally:
        server.shutdown()
        server.server_close()


def test_real_helper_reconciles_committed_post_after_unreadable_response(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"\xff")

        def do_GET(self):
            messages = [
                {
                    "from": item["did"],
                    "sig": item["sig"],
                    "nonce": int(item["nonce"]),
                    "text": item["text"],
                }
                for item in received
            ]
            body = json.dumps({"messages": messages}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home, env = _helper_env(tmp_path, server.server_port, repo)

    try:
        result = subprocess.run(
            ["bash", str(helper), "test-room", "once only"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "exact signed record is present" in result.stdout
        assert len(received) == 1
        assert not list((home / ".config" / "technocore" / "nonces").glob("*.pending"))
    finally:
        server.shutdown()
        server.server_close()


def test_real_helper_blocks_retry_while_outcome_is_unresolved(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"\xff")

        def do_GET(self):
            body = json.dumps({"messages": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home, env = _helper_env(tmp_path, server.server_port, repo)

    try:
        first = subprocess.run(
            ["bash", str(helper), "test-room", "uncertain"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert first.returncode == 2
        assert len(received) == 1
        assert len(list((home / ".config" / "technocore" / "nonces").glob("*.pending"))) == 1

        second = subprocess.run(
            ["bash", str(helper), "test-room", "uncertain"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert second.returncode == 2
        assert len(received) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_real_helper_does_not_repeat_committed_post_after_http_500(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"
    received = []
    reads = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            body = b"response failed after append"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            nonlocal reads
            reads += 1
            if reads == 1:
                body = b"reconciliation temporarily unavailable"
                self.send_response(500)
            else:
                body = json.dumps(
                    {
                        "messages": [
                            {
                                "from": item["did"],
                                "sig": item["sig"],
                                "nonce": int(item["nonce"]),
                                "text": item["text"],
                            }
                            for item in received
                        ]
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home, env = _helper_env(tmp_path, server.server_port, repo)

    try:
        first = subprocess.run(
            ["bash", str(helper), "test-room", "once despite 500"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert first.returncode == 2
        assert len(received) == 1
        assert len(list((home / ".config" / "technocore" / "nonces").glob("*.pending"))) == 1

        restarted = subprocess.run(
            ["bash", str(helper), "test-room", "once despite 500"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert restarted.returncode == 0, restarted.stderr
        assert "not sending the same logical message again" in restarted.stdout
        assert len(received) == 1
        assert not list((home / ".config" / "technocore" / "nonces").glob("*.pending"))
    finally:
        server.shutdown()
        server.server_close()
