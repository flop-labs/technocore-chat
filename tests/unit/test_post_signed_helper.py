import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def test_real_helper_serializes_concurrent_delivery(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"

    home = tmp_path / "home"
    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text("test-seed\n")
    seed_file.chmod(0o600)

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

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["TECHNOCORE_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"

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

        # B must still be blocked behind A's per-DID/per-room lock.
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
    finally:
        release_first.set()
        server.shutdown()
        server.server_close()
