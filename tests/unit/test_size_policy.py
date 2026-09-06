from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_enforcing_size_modes_reject_unlisted_src_python(tmp_path):
    shutil.copy(ROOT / "sz.py", tmp_path / "sz.py")
    shutil.copy(ROOT / "sz-baseline.json", tmp_path / "sz-baseline.json")
    shutil.copytree(ROOT / "src", tmp_path / "src")
    (tmp_path / "src" / "rogue.py").write_text("value = 1\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "sz.py", "--caps"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "size policy missing src files: src/rogue.py" in result.stderr


def test_reporting_size_mode_still_reports_without_policy_completeness(tmp_path):
    shutil.copy(ROOT / "sz.py", tmp_path / "sz.py")
    shutil.copy(ROOT / "sz-baseline.json", tmp_path / "sz-baseline.json")
    shutil.copytree(ROOT / "src", tmp_path / "src")
    (tmp_path / "src" / "rogue.py").write_text("value = 1\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "sz.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "core total (code)" in result.stdout
    assert result.stderr == ""
