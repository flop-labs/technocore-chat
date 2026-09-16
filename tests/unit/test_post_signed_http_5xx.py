import json
import os
import subprocess
import threading
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


def test_real_helper_keeps_pending_on_unproven_http_500(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))

            # Simulate a post-append server failure: the record was accepted by
            # storage, but response construction fails with a 5xx.
            body = b"post-append failure"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # The bounded reconciliation read cannot prove the appended record is
            # visible yet, so the helper must retain its write-ahead marker.
            body = json.dumps({"messages": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - the base class's spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home, env = _helper_env(tmp_path, server.server_port, repo)

    try:
        first = subprocess.run(
            ["bash", str(helper), "test-room", "once only"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert first.returncode == 2
        assert "HTTP 500" in first.stderr
        assert len(received) == 1
        pending = list((home / ".config" / "technocore" / "nonces").glob("*.pending"))
        assert len(pending) == 1

        second = subprocess.run(
            ["bash", str(helper), "test-room", "once only"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert second.returncode == 2
        assert len(received) == 1, "restart must not send a duplicate signed POST"
        assert pending[0].exists()
    finally:
        server.shutdown()
        server.server_close()
