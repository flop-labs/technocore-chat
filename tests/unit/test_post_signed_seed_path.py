import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.technocore_post_test_support import install_trusted_git

TEST_SEED = "0123456789abcdef" * 4


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("config-writable", "group/world-writable"),
        ("seed-dir-writable", "group/world-writable"),
        ("seed-symlink", "symlink"),
    ],
)
def test_post_helper_revalidates_persistent_seed_path_before_signing(
    tmp_path: Path, case: str, expected: str
) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"

    home = tmp_path / "home"
    config_dir = home / ".config"
    seed_dir = config_dir / "technocore"
    seed_dir.mkdir(parents=True)
    config_dir.chmod(0o700)
    seed_dir.chmod(0o700)

    seed_file = seed_dir / "sign_seed"
    seed_file.write_text(TEST_SEED + "\n")
    seed_file.chmod(0o600)

    if case == "config-writable":
        config_dir.chmod(0o777)
    elif case == "seed-dir-writable":
        seed_dir.chmod(0o777)
    elif case == "seed-symlink":
        seed_file.unlink()
        real_seed = home / "real_seed"
        real_seed.write_text(TEST_SEED + "\n")
        real_seed.chmod(0o600)
        seed_file.symlink_to(real_seed)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_trusted_git(bin_dir, repo)

    uv_marker = tmp_path / "uv-invoked"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path

Path(os.environ["TEST_UV_MARKER"]).write_text("called")
raise SystemExit("uv must not run for an unsafe persistent seed path")
"""
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TEST_UV_MARKER"] = str(uv_marker)
    env["TECHNOCORE_BASE_URL"] = "http://127.0.0.1:9"

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
    assert expected in combined
    assert not uv_marker.exists()
    assert not (seed_dir / "nonces").exists()
