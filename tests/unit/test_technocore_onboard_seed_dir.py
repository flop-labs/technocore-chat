import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


OFFICIAL_REPO_URL = "https://github.com/flop-labs/technocore-chat.git"


def test_existing_group_world_writable_seed_dir_fails_before_signer(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "technocore_onboard.sh"
    real_git = shutil.which("git")
    assert real_git is not None

    upstream = tmp_path / "upstream"
    subprocess.run([real_git, "init", "-q", "-b", "main", str(upstream)], check=True)
    (upstream / "scripts").mkdir()
    (upstream / "scripts" / "sign.py").write_text("# trusted signer fixture\n")
    (upstream / "pyproject.toml").write_text("# trusted dependencies fixture\n")
    (upstream / "uv.lock").write_text("# trusted lock fixture\n")

    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Onboarding test",
            "GIT_COMMITTER_NAME": "Onboarding test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    subprocess.run([real_git, "-C", str(upstream), "add", "."], check=True, env=git_env)
    subprocess.run(
        [real_git, "-C", str(upstream), "commit", "-qm", "upstream"],
        check=True,
        env=git_env,
    )

    home = tmp_path / "home"
    fake_repo = home / "technocore-chat"
    fake_repo.parent.mkdir(parents=True)
    subprocess.run([real_git, "clone", "-q", str(upstream), str(fake_repo)], check=True, env=git_env)
    subprocess.run(
        [real_git, "-C", str(fake_repo), "remote", "set-url", "origin", OFFICIAL_REPO_URL],
        check=True,
        env=git_env,
    )

    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text("preexisting-secret-seed\n")
    seed_file.chmod(0o600)
    seed_dir.chmod(0o777)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_wrapper = bin_dir / "git"
    git_wrapper.write_text(
        f"#!{sys.executable} -S\n"
        + """import os
import sys

args = sys.argv[1:]
official = "https://github.com/flop-labs/technocore-chat.git"
if "ls-remote" in args and "--get-url" not in args and official in args:
    args[args.index(official)] = os.environ["TEST_UPSTREAM_REPO"]
"""
        + f"os.execv({real_git!r}, [{real_git!r}, *args])\n"
    )
    git_wrapper.chmod(0o755)

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

    env = git_env.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "TEST_UPSTREAM_REPO": str(upstream),
            "TEST_DID_MARKER": str(did_marker),
        }
    )
    env.pop("SIGN_SEED", None)

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
    assert "it was group/world-writable" in combined
    assert "did:key:" not in combined
    assert not did_marker.exists()

    # Refusal must precede any chmod-and-trust repair. Preserve the evidence and
    # force an explicit identity recovery/rotation decision.
    assert stat.S_IMODE(seed_dir.stat().st_mode) == 0o777
    assert seed_file.read_text() == "preexisting-secret-seed\n"
    assert stat.S_IMODE(seed_file.stat().st_mode) == 0o600
