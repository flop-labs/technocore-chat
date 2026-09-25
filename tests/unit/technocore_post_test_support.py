import shutil
import subprocess
import sys
from pathlib import Path

OFFICIAL_REPO_URL = "https://github.com/flop-labs/technocore-chat.git"


def install_trusted_git(bin_dir: Path, trusted_repo: Path) -> None:
    """Keep posting-helper tests offline while exercising its real Git checks."""
    real_git = shutil.which("git")
    assert real_git is not None
    trusted_sha = subprocess.run(
        [real_git, "-C", str(trusted_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    wrapper = bin_dir / "git"
    wrapper.write_text(
        f"#!{sys.executable} -S\n"
        + f"REAL_GIT = {real_git!r}\n"
        + f"TRUSTED_SHA = {trusted_sha!r}\n"
        + f"OFFICIAL = {OFFICIAL_REPO_URL!r}\n"
        + """import os
import sys

args = sys.argv[1:]
if "remote" in args and "get-url" in args and args[-1] == "origin":
    print(OFFICIAL)
    raise SystemExit(0)
if "ls-remote" in args and "--get-url" in args:
    print(OFFICIAL)
    raise SystemExit(0)
if "ls-remote" in args and "refs/heads/main" in args:
    print(f"{TRUSTED_SHA}\\trefs/heads/main")
    raise SystemExit(0)
os.execv(REAL_GIT, [REAL_GIT, *args])
"""
    )
    wrapper.chmod(0o755)


def init_trusted_checkout(path: Path) -> None:
    """Create a committed checkout whose signer/dependencies define test upstream."""
    real_git = shutil.which("git")
    assert real_git is not None
    subprocess.run([real_git, "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        [real_git, "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        [real_git, "-C", str(path), "config", "user.name", "Technocore tests"],
        check=True,
    )
    subprocess.run([real_git, "-C", str(path), "add", "."], check=True)
    subprocess.run([real_git, "-C", str(path), "commit", "-qm", "trusted fixture"], check=True)
