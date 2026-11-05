"""Exercise the contributor's test recipe without running the suite recursively."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SELECTED = "tests/unit/test_browser_gate.py"


@pytest.mark.parametrize("selection", [(), (SELECTED,)])
def test_recipe_respects_explicit_selection_and_defaults_to_tests(selection):
    just = shutil.which("just") or str(Path(sys.executable).with_name("just"))
    result = subprocess.run(
        [just, "test", *selection, "--collect-only", "-qq"],
        cwd=ROOT,
        # The outer test run already installed its dependencies. A nested recipe must
        # not reshape that environment, especially when it is shared by other tests.
        env={**os.environ, "UV_NO_SYNC": "1", "UV_FROZEN": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = set(re.findall(r"^(tests/[^\n]+\.py): \d+$", result.stdout, re.M))
    if selection:
        assert collected == {SELECTED}, result.stdout
    else:
        assert {SELECTED, "tests/http/test_rooms.py", "tests/test_mcp.py"} <= collected
