"""queue-guard's overlap check: which PR text counts as citing an issue *here*.

The overlap job posts "Open pull requests citing the same issues" on a fork PR. Its `cites`
function is the second half of that decision (the search is only a prefilter), and it is
inline JavaScript in `.github/workflows/queue-guard.yml`, so nothing ran it before merge.
That is how one Dependabot bump came to be listed against #1061, #1100, #1114 and #1117:
its body quotes the bumped actions' release notes, `docker/login-action#1066` and
`https://redirect.github.com/astral-sh/setup-uv/issues/1113`, and the old check read every
`#N` and every `issues/N` URL tail as a reference to this repository.

This test lifts `cites` out of the workflow byte-for-byte and runs it under node, so the
workflow stays the one copy. Parsed with a regex, not a YAML library, for the same reason
as `test_browser_gate.py`.

Run: uv run --group dev python -m pytest tests/unit/test_queue_guard.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "queue-guard.yml"
NODE = shutil.which("node")

# From the first line the check depends on to the end of `cites`. Anchored on the source
# text, so moving or renaming either end fails here rather than testing nothing.
_CITES = re.compile(
    r"^( *const here = .*?^ *const cites = \(item, ref\) => \{\n.*?^ *\};\n)", re.M | re.S
)

HERE = "https://github.com/flop-labs/technocore-chat"
HUMAN = {"type": "User"}

CASES = [
    # (title, body, user, ref, expected)
    ("fix: x (#1113)", "", HUMAN, "#1113", True),
    ("", "Fixes #1113.", HUMAN, "#1113", True),
    ("", f"see {HERE}/issues/1113", HUMAN, "#1113", True),
    ("", f"builds on {HERE}/pull/1066", HUMAN, "#1066", True),
    ("", "flop-labs/technocore-chat#1090", HUMAN, "#1090", True),
    ("", "Fixes #10660", HUMAN, "#1066", False),
    ("", "measured 6,607 appends/s", HUMAN, "#607", False),
    ("", "docker/login-action#1066", HUMAN, "#1066", False),
    ("", "https://github.com/astral-sh/setup-uv/issues/1113", HUMAN, "#1113", False),
    ("", "https://redirect.github.com/astral-sh/setup-uv/issues/1113", HUMAN, "#1113", False),
    # The shape that produced the false overlaps: a bot's quoted release notes.
    (
        "ci: bump the actions group across 1 directory with 4 updates",
        '<li>Bump (<a href="https://redirect.github.com/astral-sh/setup-uv/issues/1113">#1113</a>)</li>',
        {"type": "Bot"},
        "#1113",
        False,
    ),
]


def _cites_source() -> str:
    found = _CITES.search(WORKFLOW.read_text(encoding="utf-8"))
    assert found, "queue-guard.yml no longer defines `const here` ... `const cites` in that order"
    return found.group(1)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_only_a_reference_to_this_repository_counts_as_citing():
    assert NODE is not None
    items = [[{"title": t, "body": b, "user": u}, ref] for t, b, u, ref, _ in CASES]
    script = (
        "const context = {repo: {owner: 'flop-labs', repo: 'technocore-chat'}};\n"
        + _cites_source()
        + f"\nconsole.log(JSON.stringify({json.dumps(items)}.map(([i, r]) => cites(i, r))));\n"
    )
    out = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    got = json.loads(out.stdout)
    wrong = [(c[1] or c[0], c[3], c[4]) for c, g in zip(CASES, got, strict=True) if g != c[4]]
    assert not wrong, f"cites() disagrees on (text, ref, expected): {wrong}"
