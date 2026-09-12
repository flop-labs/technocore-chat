import hashlib
import os
import stat
import subprocess
from pathlib import Path


OFFICIAL_REPO_URL = "https://github.com/flop-labs/technocore-chat.git"


def _did_from_output(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("did:key:"):
            return line.strip()
    raise AssertionError(f"no DID in output:\n{output}")


def _init_repo_with_origin(path: Path, origin: str = OFFICIAL_REPO_URL) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)


def test_two_first_run_processes_converge_on_persisted_did(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "technocore_onboard.sh"

    home = tmp_path / "home"
    fake_repo = home / "technocore-chat"
    _init_repo_with_origin(fake_repo)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    barrier = tmp_path / "sync-barrier"
    barrier.mkdir()

    # The helper only needs `uv sync` plus `uv run scripts/sign.py did` here.
    # Synchronizing the two `uv sync` calls makes both onboarding processes
    # reach first-time seed creation together and reliably exercises the race.
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import hashlib
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]

if args == ["sync"]:
    barrier = Path(os.environ["TEST_UV_SYNC_BARRIER"])
    (barrier / str(os.getpid())).write_text("")
    deadline = time.monotonic() + 5
    while len(list(barrier.iterdir())) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit("timed out waiting for concurrent onboarding")
        time.sleep(0.01)
    raise SystemExit(0)

if args == ["run", "scripts/sign.py", "did"]:
    seed = os.environ["SIGN_SEED"].strip()
    digest = hashlib.sha256(seed.encode()).hexdigest()
    print(f"did:key:{digest}")
    raise SystemExit(0)

raise SystemExit(f"unexpected uv arguments: {args!r}")
"""
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TEST_UV_SYNC_BARRIER"] = str(barrier)

    first = subprocess.Popen(
        ["bash", str(helper)],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        ["bash", str(helper)],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out_first, err_first = first.communicate(timeout=10)
    out_second, err_second = second.communicate(timeout=10)

    assert first.returncode == 0, err_first
    assert second.returncode == 0, err_second

    did_first = _did_from_output(out_first)
    did_second = _did_from_output(out_second)
    assert did_first == did_second

    seed_file = home / ".config" / "technocore" / "sign_seed"
    persisted_seed = seed_file.read_text().strip()
    expected_did = f"did:key:{hashlib.sha256(persisted_seed.encode()).hexdigest()}"

    # Neither process may observe a disposable intermediate identity: both
    # reported DIDs must derive from the single seed that remains on disk.
    assert did_first == expected_did
    assert did_second == expected_did
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o600

    outputs = [out_first, out_second]
    assert sum("New seed created." in output for output in outputs) == 1
    assert sum("Existing seed preserved." in output for output in outputs) == 1

    # Atomic publish uses private temporary candidates; none may be left behind.
    seed_dir = seed_file.parent
    assert list(seed_dir.glob(".sign_seed.*")) == []


def test_existing_seed_with_unsafe_permissions_fails_closed(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "technocore_onboard.sh"

    home = tmp_path / "home"
    fake_repo = home / "technocore-chat"
    _init_repo_with_origin(fake_repo)

    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text("preexisting-secret-seed\n")
    seed_file.chmod(0o644)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    did_marker = tmp_path / "did-invoked"

    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["sync"]:
    raise SystemExit(0)
if args == ["run", "scripts/sign.py", "did"]:
    Path(os.environ["TEST_DID_MARKER"]).write_text("called")
    print("did:key:should-not-be-reported")
    raise SystemExit(0)
raise SystemExit(f"unexpected uv arguments: {args!r}")
"""
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TEST_DID_MARKER"] = str(did_marker)

    result = subprocess.run(
        ["bash", str(helper)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Refusing to use this seed because it may already have been exposed." in combined
    assert "did:key:" not in combined
    assert not did_marker.exists()
    assert seed_file.read_text() == "preexisting-secret-seed\n"
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o644


def test_existing_checkout_with_untrusted_origin_fails_before_code_execution(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "technocore_onboard.sh"

    home = tmp_path / "home"
    fake_repo = home / "technocore-chat"
    _init_repo_with_origin(fake_repo, "https://github.com/example/untrusted-technocore.git")

    scripts_dir = fake_repo / "scripts"
    scripts_dir.mkdir()
    signer_marker = tmp_path / "signer-observed-seed"
    (scripts_dir / "sign.py").write_text(
        """import os
from pathlib import Path

seed = os.environ.get("SIGN_SEED")
if seed is not None:
    Path(os.environ["TEST_SIGNER_MARKER"]).write_text(seed)
raise SystemExit("sentinel signer should never execute")
"""
    )

    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text("preexisting-secret-seed\n")
    seed_file.chmod(0o600)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_marker = tmp_path / "uv-invoked"

    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

Path(os.environ["TEST_UV_MARKER"]).write_text(" ".join(sys.argv[1:]))
if sys.argv[1:] == ["run", "scripts/sign.py", "did"]:
    os.execv(sys.executable, [sys.executable, "scripts/sign.py"])
raise SystemExit(0)
"""
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TEST_UV_MARKER"] = str(uv_marker)
    env["TEST_SIGNER_MARKER"] = str(signer_marker)

    result = subprocess.run(
        ["bash", str(helper)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "refusing to use existing checkout" in combined
    assert "origin is not the official flop-labs/technocore-chat repository" in combined
    assert "https://github.com/example/untrusted-technocore.git" in combined
    assert not uv_marker.exists()
    assert not signer_marker.exists()
    assert seed_file.read_text() == "preexisting-secret-seed\n"
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o600
