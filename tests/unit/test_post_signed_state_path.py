import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.technocore_post_test_support import install_trusted_git

TEST_SEED = "0123456789abcdef" * 4
TEST_DID = "did:test-state-path"
ROOM = "state-path-room"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("writable-dir-with-state-symlink", "group/world-writable"),
        ("state-symlink", "nonce state file is a symlink"),
        ("pending-symlink", "pending outcome marker is a symlink"),
    ],
)
def test_post_helper_rejects_untrusted_nonce_state_before_mutation(
    tmp_path: Path, case: str, expected: str
) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"

    home = tmp_path / "home"
    config_dir = home / ".config"
    seed_dir = config_dir / "technocore"
    state_dir = seed_dir / "nonces"
    state_dir.mkdir(parents=True)
    config_dir.chmod(0o700)
    seed_dir.chmod(0o700)
    state_dir.chmod(0o700)

    seed_file = seed_dir / "sign_seed"
    seed_file.write_text(TEST_SEED + "\n")
    seed_file.chmod(0o600)

    key = hashlib.sha256((TEST_DID + "\0" + ROOM).encode()).hexdigest()
    target = tmp_path / "must-not-touch"
    target.write_text("preserve me\n")
    target.chmod(0o600)

    if case == "writable-dir-with-state-symlink":
        (state_dir / key).symlink_to(target)
        state_dir.chmod(0o777)
    elif case == "state-symlink":
        (state_dir / key).symlink_to(target)
    elif case == "pending-symlink":
        (state_dir / f"{key}.pending").symlink_to(target)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_trusted_git(bin_dir, repo)

    say_marker = tmp_path / "say-invoked"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

if sys.argv[-1] == "did":
    print({TEST_DID!r})
    raise SystemExit(0)
Path(os.environ["TEST_SAY_MARKER"]).write_text("called")
raise SystemExit("signing must not reach say for unsafe nonce state")
"""
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TEST_SAY_MARKER"] = str(say_marker)
    env["TECHNOCORE_BASE_URL"] = "http://127.0.0.1:9"

    result = subprocess.run(
        ["bash", str(helper), ROOM, "must-not-send"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert expected in combined
    assert target.read_text() == "preserve me\n"
    assert not say_marker.exists()

    if case == "writable-dir-with-state-symlink":
        assert state_dir.stat().st_mode & 0o022
