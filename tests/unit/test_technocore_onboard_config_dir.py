import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


OFFICIAL_REPO_URL = "https://github.com/flop-labs/technocore-chat.git"


def test_existing_group_world_writable_config_dir_fails_before_seed_or_signer(tmp_path) -> None:
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

    config_dir = home / ".config"
    config_dir.mkdir()
    config_dir.chmod(0o777)
    seed_dir = config_dir / "technocore"

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

    uv_marker = tmp_path / "uv-invoked"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path

Path(os.environ["TEST_UV_MARKER"]).write_text("called")
raise SystemExit("uv must not run when the config parent is unsafe")
"""
    )
    fake_uv.chmod(0o755)

    env = git_env.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "TEST_UPSTREAM_REPO": str(upstream),
            "TEST_UV_MARKER": str(uv_marker),
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
    assert "config directory permissions are 777; it was group/world-writable" in combined
    assert "did:key:" not in combined
    assert not uv_marker.exists()
    assert not seed_dir.exists()

    # Refusal must preserve the unsafe parent as evidence rather than silently
    # repairing it and trusting a child identity path that may have been swapped.
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o777
