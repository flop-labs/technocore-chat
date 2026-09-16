import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.technocore_post_test_support import install_trusted_git


@pytest.mark.parametrize(
    "seed_text",
    [
        "a" * 63 + "\n",
        "not-a-generated-seed\n",
        "A" * 64 + "\n",
        "a" * 64,
    ],
    ids=["truncated", "text", "uppercase", "missing-newline"],
)
def test_post_helper_rejects_invalid_persisted_seed_before_signing(
    tmp_path: Path, seed_text: str
) -> None:
    repo = Path(__file__).resolve().parents[2]
    helper = repo / "post_signed.sh"

    home = tmp_path / "home"
    seed_dir = home / ".config" / "technocore"
    seed_dir.mkdir(parents=True)
    seed_dir.chmod(0o700)
    seed_file = seed_dir / "sign_seed"
    seed_file.write_text(seed_text)
    seed_file.chmod(0o600)

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
raise SystemExit("uv must not run for an invalid persistent seed")
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
    assert "not the generated 64-lowercase-hex format" in combined
    assert not uv_marker.exists()
    assert not (seed_dir / "nonces").exists()
