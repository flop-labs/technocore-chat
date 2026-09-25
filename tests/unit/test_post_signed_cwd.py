import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.unit.technocore_post_test_support import init_trusted_checkout, install_trusted_git

TEST_SEED = "0123456789abcdef" * 4


@pytest.mark.parametrize("invocation", ["absolute", "relative"])
@pytest.mark.parametrize("caller_has_signer", [False, True], ids=["empty-cwd", "decoy-signer"])
def test_posting_uses_own_checkout_from_unrelated_cwd(
    tmp_path: Path, invocation: str, caller_has_signer: bool
) -> None:
    repo = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "helper checkout with spaces"
    (checkout / "scripts").mkdir(parents=True)
    helper = checkout / "post_signed.sh"
    shutil.copy2(repo / "post_signed.sh", helper)

    (checkout / "scripts" / "sign.py").write_text(
        f"""import os
import sys

assert os.environ["SIGN_SEED"] == {TEST_SEED!r}
if sys.argv[1:] == ["did"]:
    print("did:key:helper-fixture")
elif sys.argv[1] == "say" and len(sys.argv) == 5:
    print("did:key:helper-fixture")
    print("fixture-signature")
else:
    raise SystemExit("unexpected signer arguments")
"""
    )
    (checkout / "pyproject.toml").write_text("# trusted dependency fixture\n")
    (checkout / "uv.lock").write_text("# trusted lock fixture\n")
    init_trusted_checkout(checkout)

    caller = tmp_path / "unrelated working directory"
    caller.mkdir()
    caller_marker = tmp_path / "caller-signer-ran"
    if caller_has_signer:
        (caller / "scripts").mkdir()
        (caller / "scripts" / "sign.py").write_text(
            """import os
from pathlib import Path

Path(os.environ["TEST_CALLER_MARKER"]).write_text("called")
raise SystemExit("caller-controlled signer must not execute")
"""
        )

    home = tmp_path / "home"
    seed_file = home / ".config" / "technocore" / "sign_seed"
    seed_file.parent.mkdir(parents=True)
    seed_file.write_text(TEST_SEED + "\n")
    seed_file.chmod(0o600)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_trusted_git(bin_dir, checkout)
    uv_log = tmp_path / "uv-calls.jsonl"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["TEST_UV_LOG"]).open("a") as log:
    log.write(json.dumps({"cwd": str(Path.cwd()), "args": args}) + "\\n")
if args[:4] != ["run", "--frozen", "python", "scripts/sign.py"]:
    raise SystemExit("unexpected uv arguments")
os.execv(sys.executable, [sys.executable, *args[3:]])
"""
    )
    fake_uv.chmod(0o755)

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, payload))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):  # noqa: A002 - the base class's own spelling
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        TECHNOCORE_BASE_URL=f"http://127.0.0.1:{server.server_port}",
        TEST_CALLER_MARKER=str(caller_marker),
        TEST_UV_LOG=str(uv_log),
    )
    command = str(helper) if invocation == "absolute" else os.path.relpath(helper, caller)
    message = "hello from a different folder"
    try:
        result = subprocess.run(
            [command, "test-room", message],
            cwd=caller,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert TEST_SEED not in result.stdout + result.stderr
    assert not caller_marker.exists()
    calls = [json.loads(line) for line in uv_log.read_text().splitlines()]
    assert len(calls) == 2
    assert all(call["cwd"] == str(checkout.resolve()) for call in calls)
    assert calls[0]["args"] == ["run", "--frozen", "python", "scripts/sign.py", "did"]
    assert len(received) == 1
    request_path, payload = received[0]
    assert request_path == "/r/test-room"
    assert payload == {
        "did": "did:key:helper-fixture",
        "sig": "fixture-signature",
        "nonce": calls[1]["args"][6],
        "text": message,
    }
    assert calls[1]["args"] == [
        "run",
        "--frozen",
        "python",
        "scripts/sign.py",
        "say",
        "test-room",
        payload["nonce"],
        message,
    ]
    nonce_files = list((seed_file.parent / "nonces").iterdir())
    assert len(nonce_files) == 1
    assert int(nonce_files[0].read_text()) == int(payload["nonce"]) > 0
    assert seed_file.read_text() == TEST_SEED + "\n"
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o600


def test_posting_rejects_modified_sibling_signer_before_signing(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "trusted-checkout"
    (checkout / "scripts").mkdir(parents=True)
    helper = checkout / "post_signed.sh"
    shutil.copy2(repo / "post_signed.sh", helper)
    (checkout / "scripts" / "sign.py").write_text("# trusted signer\n")
    (checkout / "pyproject.toml").write_text("# trusted dependency fixture\n")
    (checkout / "uv.lock").write_text("# trusted lock fixture\n")
    init_trusted_checkout(checkout)

    signer_marker = tmp_path / "modified-signer-ran"
    (checkout / "scripts" / "sign.py").write_text(
        """import os
from pathlib import Path

Path(os.environ["TEST_SIGNER_MARKER"]).write_text("called")
raise SystemExit("modified signer must never execute")
"""
    )

    home = tmp_path / "home"
    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text(TEST_SEED + "\n")
    seed_file.chmod(0o600)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_trusted_git(bin_dir, checkout)
    uv_marker = tmp_path / "uv-invoked"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\n"
        + """import os
from pathlib import Path

Path(os.environ["TEST_UV_MARKER"]).write_text("called")
raise SystemExit("uv must not run for a modified signing snapshot")
"""
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        TEST_SIGNER_MARKER=str(signer_marker),
        TEST_UV_MARKER=str(uv_marker),
        TECHNOCORE_BASE_URL="http://127.0.0.1:9",
    )
    result = subprocess.run(
        ["bash", str(helper), "test-room", "must-not-send"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "scripts/sign.py differs from verified upstream content" in combined
    assert TEST_SEED not in combined
    assert not uv_marker.exists()
    assert not signer_marker.exists()
    assert not (seed_dir / "nonces").exists()
