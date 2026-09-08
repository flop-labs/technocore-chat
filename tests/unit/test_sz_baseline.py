"""Run: uv run --group dev python -m pytest tests

sz-baseline.json used to store `core_total` as a top-level value even though it is always
the sum of the per-file `code_lines` rows beside it, and `--update-baseline` rewrote every
core file's row from a fresh measurement on every run regardless of whether that file
changed. Two PRs growing different core files then collided on rows neither one touched,
and a comment-only edit rewrote a row that no check ever reads past `code_lines` (see the
flop-labs/technocore-chat issue #254 discussion this fixes).

This runs the real sz.py against an isolated copy of the tree, not a reimplementation of
its merge logic, so a regression here means the shipped tool actually collides again.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC_FILES = ("app.py", "config.py", "didkey.py", "limit.py", "store.py")


def _sandbox(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "sz.py", root / "sz.py")
    shutil.copy(REPO_ROOT / "sz-baseline.json", root / "sz-baseline.json")
    for name in (*CORE_SRC_FILES, "manifest.py"):
        shutil.copy(REPO_ROOT / "src" / name, root / "src" / name)
    return root


def _update_baseline(root):
    subprocess.run(
        [sys.executable, "sz.py", "--update-baseline"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads((root / "sz-baseline.json").read_text(encoding="utf-8"))


def test_update_baseline_never_writes_a_top_level_core_total(tmp_path):
    written = _update_baseline(_sandbox(tmp_path))
    assert "core_total" not in written
    # The cap is policy and stays; only the ratchet's cached copy of the sum is gone.
    assert "core_total" in written["caps"]


def test_update_baseline_preserves_a_row_whose_code_lines_did_not_move(tmp_path):
    root = _sandbox(tmp_path)
    before = _update_baseline(root)

    didkey = root / "src" / "didkey.py"
    edited = "# unrelated comment-only edit, adds a line without adding code\n" + didkey.read_text(
        encoding="utf-8"
    )
    didkey.write_text(edited, encoding="utf-8")
    edited_line_count = len(edited.splitlines())

    after = _update_baseline(root)

    stored = after["files"]["core/didkey.py"]
    assert stored == before["files"]["core/didkey.py"]
    # Proves the row was preserved rather than a fresh measurement that happens to match:
    # the edit really did add a line, and the stored raw_lines does not reflect it.
    assert stored["raw_lines"] != edited_line_count

    # A file nobody touched keeps its row too, so this is a per-file comparison rather
    # than an accidental skip of the whole rewrite.
    assert after["files"]["core/app.py"] == before["files"]["core/app.py"]


def test_update_baseline_writes_a_fresh_row_when_code_lines_actually_changed(tmp_path):
    root = _sandbox(tmp_path)
    before = _update_baseline(root)

    didkey = root / "src" / "didkey.py"
    didkey.write_text(
        didkey.read_text(encoding="utf-8") + "\nSZ_TEST_MARKER = 1\n", encoding="utf-8"
    )

    after = _update_baseline(root)

    before_row = before["files"]["core/didkey.py"]
    after_row = after["files"]["core/didkey.py"]
    assert after_row["code_lines"] == before_row["code_lines"] + 1
    assert after_row != before_row
