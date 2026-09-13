import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

OFFICIAL_REPO_URL = "https://github.com/flop-labs/technocore-chat.git"


def _did_from_output(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("did:key:"):
            return line.strip()
    raise AssertionError(f"no DID in output:\n{output}")


@pytest.fixture(autouse=True)
def _local_upstream_transport(tmp_path, monkeypatch) -> None:
    """Use real Git objects/status, replacing only the official network transport."""
    real_git = shutil.which("git")
    assert real_git is not None
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Onboarding test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Onboarding test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.delenv("SIGN_SEED", raising=False)

    upstream = tmp_path / "upstream"
    subprocess.run([real_git, "init", "-q", "-b", "main", str(upstream)], check=True)
    (upstream / "scripts").mkdir()
    (upstream / "scripts" / "sign.py").write_text("# trusted signer fixture\n")
    (upstream / "pyproject.toml").write_text("# trusted dependencies fixture\n")
    (upstream / "uv.lock").write_text("# trusted lock fixture\n")
    subprocess.run([real_git, "-C", str(upstream), "add", "."], check=True)
    subprocess.run([real_git, "-C", str(upstream), "commit", "-qm", "upstream"], check=True)
    monkeypatch.setenv("TEST_UPSTREAM_REPO", str(upstream))

    transport_bin = tmp_path / "git-bin"
    transport_bin.mkdir()
    git_wrapper = transport_bin / "git"
    git_wrapper.write_text(
        f"#!{sys.executable} -S\n"
        + """import os
import sys

args = sys.argv[1:]
official = "https://github.com/flop-labs/technocore-chat.git"
if "ls-remote" in args and "--get-url" not in args:
    if os.environ.get("TEST_UPSTREAM_QUERY_FAIL"):
        raise SystemExit(128)
    if "TEST_UPSTREAM_RESPONSE" in os.environ:
        print(os.environ["TEST_UPSTREAM_RESPONSE"])
        raise SystemExit(0)
    assert official in args, "verification must query the official URL, not a local ref"
    args[args.index(official)] = os.environ["TEST_UPSTREAM_REPO"]
if "clone" in args and official in args:
    args[args.index(official)] = os.environ["TEST_UPSTREAM_REPO"]
"""
        + f"os.execv({real_git!r}, [{real_git!r}, *args])\n"
    )
    git_wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{transport_bin}{os.pathsep}{os.environ['PATH']}")


def _init_repo_with_origin(path: Path, origin: str = OFFICIAL_REPO_URL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", os.environ["TEST_UPSTREAM_REPO"], str(path)], check=True
    )
    subprocess.run(["git", "-C", str(path), "remote", "set-url", "origin", origin], check=True)


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

    # The helper only needs `uv sync --frozen` plus `uv run --frozen scripts/sign.py did` here.
    # Synchronizing the two `uv sync --frozen` calls makes both onboarding processes
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

if args == ["sync", "--frozen"]:
    barrier = Path(os.environ["TEST_UV_SYNC_BARRIER"])
    (barrier / str(os.getpid())).write_text("")
    deadline = time.monotonic() + 5
    while len(list(barrier.iterdir())) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit("timed out waiting for concurrent onboarding")
        time.sleep(0.01)
    raise SystemExit(0)

if args == ["run", "--frozen", "scripts/sign.py", "did"]:
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


def test_loser_makes_winning_seed_directory_entry_durable_before_success(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "technocore_onboard.sh"

    home = tmp_path / "home"
    fake_repo = home / "technocore-chat"
    _init_repo_with_origin(fake_repo)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_barrier = tmp_path / "uv-barrier"
    uv_barrier.mkdir()
    publish_sync = tmp_path / "publish-sync"
    publish_sync.mkdir()

    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import hashlib
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args == ["sync", "--frozen"]:
    barrier = Path(os.environ["TEST_UV_SYNC_BARRIER"])
    (barrier / str(os.getpid())).write_text("")
    deadline = time.monotonic() + 5
    while len(list(barrier.iterdir())) < 2:
        if time.monotonic() >= deadline:
            raise SystemExit("timed out waiting for concurrent onboarding")
        time.sleep(0.01)
    raise SystemExit(0)
if args == ["run", "--frozen", "scripts/sign.py", "did"]:
    seed = os.environ["SIGN_SEED"].strip()
    print(f"did:key:{hashlib.sha256(seed.encode()).hexdigest()}")
    raise SystemExit(0)
raise SystemExit(f"unexpected uv arguments: {args!r}")
"""
    )
    fake_uv.chmod(0o755)

    # Interpose only the helper's embedded `python3 -` seed publisher. The
    # process that wins os.link() is paused before it can execute its own
    # directory fsync. The losing publisher can therefore finish only if its
    # FileExistsError path fsyncs the parent directory itself.
    real_python = sys.executable
    runner = tmp_path / "seed_runner.py"
    runner.write_text(
        """import os
import stat
import sys
import time
from pathlib import Path

sync = Path(os.environ["TEST_PUBLISH_SYNC"])
real_link = os.link
real_fsync = os.fsync

def link(src, dst, *args, **kwargs):
    try:
        result = real_link(src, dst, *args, **kwargs)
    except FileExistsError:
        (sync / "loser-linked").write_text(str(os.getpid()))
        raise
    (sync / "winner-linked").write_text(str(os.getpid()))
    deadline = time.monotonic() + 8
    while not (sync / "release-winner").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting to release winner")
        time.sleep(0.01)
    return result

def fsync(fd):
    result = real_fsync(fd)
    try:
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
    except OSError:
        is_dir = False
    if is_dir and (sync / "loser-linked").exists():
        loser_pid = (sync / "loser-linked").read_text().strip()
        if loser_pid == str(os.getpid()):
            (sync / "loser-dir-fsynced").write_text(loser_pid)
    return result

os.link = link
os.fsync = fsync
code = sys.stdin.read()
exec(compile(code, "<stdin>", "exec"), {"__name__": "__main__"})
"""
    )

    python_wrapper = bin_dir / "python3"
    python_wrapper.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "-" ]]; then
  exec {real_python!s} {runner!s}
fi
exec {real_python!s} "$@"
"""
    )
    python_wrapper.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TEST_UV_SYNC_BARRIER"] = str(uv_barrier)
    env["TEST_PUBLISH_SYNC"] = str(publish_sync)

    processes = [
        subprocess.Popen(
            ["bash", str(helper)],
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]

    try:
        deadline = time.monotonic() + 8
        while not (publish_sync / "winner-linked").exists():
            assert time.monotonic() < deadline, "winner never published sign_seed"
            time.sleep(0.01)
        while not (publish_sync / "loser-linked").exists():
            assert time.monotonic() < deadline, "loser never observed published sign_seed"
            time.sleep(0.01)
        while not (publish_sync / "loser-dir-fsynced").exists():
            assert time.monotonic() < deadline, "loser did not fsync seed directory"
            time.sleep(0.01)

        # Winner remains deliberately blocked inside os.link(). The other helper
        # must nevertheless be able to finish and report the durable DID.
        loser_proc = None
        while loser_proc is None:
            assert time.monotonic() < deadline, "loser did not complete before winner release"
            for process in processes:
                if process.poll() is not None:
                    loser_proc = process
                    break
            if loser_proc is None:
                time.sleep(0.01)

        loser_out, loser_err = loser_proc.communicate(timeout=1)
        assert loser_proc.returncode == 0, loser_err
        assert "Existing seed preserved." in loser_out
        loser_did = _did_from_output(loser_out)

        winner_proc = processes[0] if processes[1] is loser_proc else processes[1]
        assert winner_proc.poll() is None

        seed_file = home / ".config" / "technocore" / "sign_seed"
        persisted_seed = seed_file.read_text().strip()
        expected_did = f"did:key:{hashlib.sha256(persisted_seed.encode()).hexdigest()}"
        assert loser_did == expected_did
    finally:
        (publish_sync / "release-winner").write_text("go")
        for process in processes:
            if process.poll() is None:
                process.communicate(timeout=5)


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
if args == ["sync", "--frozen"]:
    raise SystemExit(0)
if args == ["run", "--frozen", "scripts/sign.py", "did"]:
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
    scripts_dir.mkdir(exist_ok=True)
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
if sys.argv[1:] == ["run", "--frozen", "scripts/sign.py", "did"]:
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


def test_existing_official_origin_with_untrusted_local_commit_fails_before_code_execution(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "technocore_onboard.sh"

    home = tmp_path / "home"
    fake_repo = home / "technocore-chat"
    _init_repo_with_origin(fake_repo)

    scripts_dir = fake_repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
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
    subprocess.run(["git", "-C", str(fake_repo), "add", "scripts/sign.py"], check=True)
    subprocess.run(
        ["git", "-C", str(fake_repo), "commit", "-q", "-m", "local untrusted signer"],
        check=True,
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
if sys.argv[1:] == ["run", "--frozen", "scripts/sign.py", "did"]:
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
    assert "HEAD does not match verified upstream main" in combined
    assert not uv_marker.exists()
    assert not signer_marker.exists()
    assert seed_file.read_text() == "preexisting-secret-seed\n"
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o600


SENTINEL_SIGNER = """import os
from pathlib import Path

if os.environ.get("SIGN_SEED"):
    Path(os.environ["TEST_SIGNER_MARKER"]).write_text("seed observed")
raise SystemExit("untrusted signer must not execute")
"""


def _trust_case(tmp_path, *, existing_checkout=True):
    home = tmp_path / "home"
    checkout = home / "technocore-chat"
    if existing_checkout:
        _init_repo_with_origin(checkout)
    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text("onboarding-test-fixture-not-a-real-key\n")
    seed_file.chmod(0o600)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_marker = tmp_path / "uv-invoked"
    signer_marker = tmp_path / "signer-observed-seed"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable} -S\n"
        + """import hashlib
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["TEST_UV_MARKER"]).open("a") as marker:
    marker.write(" ".join(args) + "\\n")
args = [arg for arg in args if arg != "--frozen"]
if args == ["sync"]:
    if os.environ.get("TEST_MODIFY_DURING_SYNC"):
        Path("scripts/sign.py").write_text("# modified during sync\\n")
    raise SystemExit(0)
if args == ["run", "scripts/sign.py", "did"]:
    if Path("scripts/sign.py").read_text() != "# trusted signer fixture\\n":
        os.execv(sys.executable, [sys.executable, "scripts/sign.py"])
    seed = os.environ["SIGN_SEED"].strip()
    print(f"did:key:{hashlib.sha256(seed.encode()).hexdigest()}")
    raise SystemExit(0)
raise SystemExit(f"unexpected uv arguments: {args!r}")
"""
    )
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        TEST_UV_MARKER=str(uv_marker),
        TEST_SIGNER_MARKER=str(signer_marker),
    )
    return checkout, seed_file, uv_marker, signer_marker, env


def _run_onboarding(env):
    repo = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["bash", str(repo / "technocore_onboard.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _assert_refused(result, seed_file, uv_marker, signer_marker) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "did:key:" not in combined
    assert "Setup complete." not in combined
    assert not uv_marker.exists()
    assert not signer_marker.exists()
    assert seed_file.read_text() == "onboarding-test-fixture-not-a-real-key\n"
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "change", ["unstaged", "staged", "local-commit", "forged-tracking-ref"]
)
def test_official_origin_does_not_trust_modified_or_local_signer(tmp_path, change) -> None:
    checkout, seed, uv_marker, signer_marker, env = _trust_case(tmp_path)
    signer = checkout / "scripts" / "sign.py"
    signer.write_text(SENTINEL_SIGNER)
    if change != "unstaged":
        subprocess.run(["git", "-C", str(checkout), "add", "scripts/sign.py"], check=True)
    if change in {"local-commit", "forged-tracking-ref"}:
        subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "local signer"], check=True)
    if change == "forged-tracking-ref":
        subprocess.run(
            ["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main", "HEAD"],
            check=True,
        )
    before_head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    result = _run_onboarding(env)
    _assert_refused(result, seed, uv_marker, signer_marker)
    assert signer.read_text() == SENTINEL_SIGNER
    assert subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == before_head


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "scripts/extra.py"])
def test_modified_dependencies_or_untracked_code_are_refused(tmp_path, path) -> None:
    checkout, seed, uv_marker, signer_marker, env = _trust_case(tmp_path)
    (checkout / path).write_text("# local modification\n")
    result = _run_onboarding(env)
    _assert_refused(result, seed, uv_marker, signer_marker)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_signer_raw_content_check_cannot_be_hidden_by_index_flags(tmp_path, flag) -> None:
    checkout, seed, uv_marker, signer_marker, env = _trust_case(tmp_path)
    subprocess.run(["git", "-C", str(checkout), "update-index", flag, "scripts/sign.py"], check=True)
    (checkout / "scripts" / "sign.py").write_text(SENTINEL_SIGNER)
    assert subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"]) == b""
    result = _run_onboarding(env)
    _assert_refused(result, seed, uv_marker, signer_marker)
    assert "scripts/sign.py differs from verified upstream content" in result.stderr


@pytest.mark.parametrize("response", [None, "", "not-a-commit\trefs/heads/main"])
def test_unavailable_or_invalid_upstream_proof_fails_closed(tmp_path, response) -> None:
    _, seed, uv_marker, signer_marker, env = _trust_case(tmp_path)
    if response is None:
        env["TEST_UPSTREAM_QUERY_FAIL"] = "1"
    else:
        env["TEST_UPSTREAM_RESPONSE"] = response
    result = _run_onboarding(env)
    _assert_refused(result, seed, uv_marker, signer_marker)


@pytest.mark.parametrize("existing_checkout", [True, False])
def test_clean_verified_upstream_preserves_existing_identity(tmp_path, existing_checkout) -> None:
    _, seed, uv_marker, signer_marker, env = _trust_case(
        tmp_path, existing_checkout=existing_checkout
    )
    before = seed.read_text()
    result = _run_onboarding(env)
    assert result.returncode == 0, result.stderr
    assert "Setup complete." in result.stdout
    assert seed.read_text() == before
    assert _did_from_output(result.stdout) == f"did:key:{hashlib.sha256(before.strip().encode()).hexdigest()}"
    assert uv_marker.read_text().splitlines() == [
        "sync --frozen",
        "run --frozen scripts/sign.py did",
    ]
    assert not signer_marker.exists()


def test_checkout_is_rechecked_before_seed_reaches_signer(tmp_path) -> None:
    _, seed, uv_marker, signer_marker, env = _trust_case(tmp_path)
    env["TEST_MODIFY_DURING_SYNC"] = "1"
    before = seed.read_text()
    result = _run_onboarding(env)
    assert result.returncode != 0
    assert "did:key:" not in result.stdout
    assert "Setup complete." not in result.stdout
    assert uv_marker.read_text().splitlines() == ["sync --frozen"]
    assert not signer_marker.exists()
    assert seed.read_text() == before


def test_official_url_cannot_be_rewritten_to_a_local_signer(tmp_path) -> None:
    checkout, seed, uv_marker, signer_marker, env = _trust_case(tmp_path)
    (checkout / "scripts" / "sign.py").write_text(SENTINEL_SIGNER)
    subprocess.run(["git", "-C", str(checkout), "add", "scripts/sign.py"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "local signer"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "config",
            f"url.{checkout.as_uri()}.insteadOf",
            OFFICIAL_REPO_URL,
        ],
        check=True,
    )
    # Model the destination Git's insteadOf rule would actually contact.
    env["TEST_UPSTREAM_REPO"] = str(checkout)
    result = _run_onboarding(env)
    _assert_refused(result, seed, uv_marker, signer_marker)
