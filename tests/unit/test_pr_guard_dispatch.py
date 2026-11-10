"""Exercise the regression guard after a draft is made ready without another push."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-guards.yml"


def test_ready_fix_without_changed_tests_is_rejected(tmp_path):
    workflow = WORKFLOW.read_text()
    events = re.search(r"^    types: \[([^\]]+)\]", workflow, re.M)
    assert events is not None
    subscribed = {event.strip() for event in events[1].split(",")}

    # Both SHAs name the same tree: this fix has no regression test. A draft's fix job
    # is skipped by its `if`, and clicking Ready is the only subsequent event here.
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    summary = tmp_path / "summary.md"
    result = subprocess.CompletedProcess([], 0, "guard was not dispatched", "")
    if "ready_for_review" in subscribed:
        # Execute the workflow's actual guard, including its real git diff. On this
        # input it refuses before checkout or dependency installation, so the repository
        # stays untouched. No GitHub API or token is involved.
        job = workflow.split("  fix-has-a-failing-test:\n", 1)[1]
        script = re.search(r"^        run: \|\n((?:          .*\n|\n)+)", job, re.M)
        assert script is not None
        result = subprocess.run(
            ["bash", "-c", textwrap.dedent(script[1])],
            cwd=ROOT,
            env=dict(os.environ, BASE_INPUT=sha, HEAD_SHA=sha, GITHUB_STEP_SUMMARY=str(summary)),
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "no file under tests/ changed" in result.stdout
    assert "**FAIL**" in summary.read_text()
