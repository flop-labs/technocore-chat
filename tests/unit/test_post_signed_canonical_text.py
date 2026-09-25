"""Exercise real signing and server sweeping across an outcome-unknown restart."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from didkey import verify
from store import clean_text
from tests.unit.test_post_signed_helper import _helper_env


@pytest.mark.parametrize(
    "raw_text",
    ["  once only  ", "\tonce\nonly\r\n", "  café\u200bonce\ue000only\u2028done\u2029  "],
    ids=["trim", "controls", "unicode"],
)
@pytest.mark.parametrize("failure", ["disconnect", "http-500"])
@pytest.mark.parametrize("legacy_marker", [False, True], ids=["new-marker", "legacy-marker"])
def test_swept_post_reconciles_on_restart_without_duplicate_or_blocking(
    tmp_path: Path, raw_text: str, failure: str, legacy_marker: bool
) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"
    canonical = clean_text(raw_text)
    assert canonical != raw_text
    received = []
    messages = []
    visible = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            stored_text = clean_text(payload["text"])
            # Verify the actual signature against server-canonical bytes, not a
            # mock signature or a room that just echoes the submitted raw text.
            verify(payload["did"], payload["sig"], f"test-room|{payload['nonce']}|{stored_text}")
            received.append(payload)
            messages.append(
                {
                    "from": payload["did"],
                    "sig": payload["sig"],
                    "nonce": int(payload["nonce"]),
                    "text": stored_text,
                }
            )
            if len(received) == 1:
                if failure == "disconnect":
                    self.close_connection = True
                    return
                self.send_response(500)
            else:
                self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_GET(self):
            body = json.dumps({"messages": messages if visible.is_set() else []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - the base class's spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home, env = _helper_env(tmp_path, server.server_port, repo)
    state_dir = home / ".config" / "technocore" / "nonces"

    def run(message):
        return subprocess.run(
            ["bash", str(helper), "test-room", message],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    try:
        first = run(raw_text)
        assert first.returncode == 2, first.stderr
        assert len(received) == 1
        (pending_file,) = state_dir.glob("*.pending")
        saved = json.loads(pending_file.read_text())

        if legacy_marker:
            # A pre-fix helper stored the raw text beside the same valid
            # signature. Upgrading must reconcile it without manual deletion.
            saved["text"] = raw_text
            pending_file.write_text(json.dumps(saved))

        blocked = run(raw_text)
        assert blocked.returncode == 2, blocked.stderr
        assert len(received) == 1
        assert pending_file.exists()

        visible.set()
        # Both an exact CLI retry and a different spelling of the same swept
        # message must suppress a duplicate after recovering the pending write.
        retry_text = raw_text if failure == "disconnect" else canonical
        restarted = run(retry_text)
        assert restarted.returncode == 0, restarted.stderr
        assert "not sending the same logical message again" in restarted.stdout
        assert len(received) == 1
        assert not pending_file.exists()
        assert received[0]["text"] == canonical
        if not legacy_marker:
            assert saved["text"] == canonical

        later = run("  a different\nmessage  ")
        assert later.returncode == 0, later.stderr
        assert len(received) == 2
        assert received[1]["text"] == "a different message"
        assert int(received[1]["nonce"]) > int(received[0]["nonce"])
        assert not list(state_dir.glob("*.pending"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
