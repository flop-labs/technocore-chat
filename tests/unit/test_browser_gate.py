"""The browser gate's two hand-kept lists, and the Python dependency line it must not touch.

`/humans` is the one surface a Python test cannot actually exercise: every assertion in
`tests/http/test_humans.py` is about the *served bytes*, and all of them pass with the
page's JavaScript completely broken. So the real gate is `tests/humans_ui_probe.mjs` in a
real browser, and `.github/workflows/humans.yml` is what makes it run.

That workflow has two properties worth pinning, both of which fail silently.

The path filter is written out twice, because GitHub Actions' parser does not support YAML
anchors — so the obvious de-duplication is a workflow that stops filtering rather than one
that shares a list. Two hand-kept copies of the same thing is the drift
`tests/edge/test_edge_worker.py` exists to catch one directory over, and this is the same
shape: edit one list, forget the other, and the gate runs on pushes but not on the pull
requests it was added for.

And the whole reason the browser gate lives in its own workflow with its own `npm ci` is
that the service and its suite stay pure Python with three pinned packages. A `playwright`
that drifts into `pyproject.toml` would be nobody's deliberate decision and nothing else
would notice.

Parsed with a regex rather than a YAML library on purpose: PyYAML is available here only as
somebody else's transitive dependency, and a test whose subject is "no dependency crept in"
should not itself lean on one that could vanish.

Run: uv run --group dev python -m pytest tests/unit/test_browser_gate.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "humans.yml"
PACKAGE = ROOT / "tests" / "package.json"
LOCKFILE = ROOT / "tests" / "package-lock.json"

# A `paths:` key followed by its `- "…"` items. Anchored to the quoted form the workflow
# uses, so a bare (unquoted) entry added later fails loudly here rather than being skipped.
_PATHS_BLOCK = re.compile(r"^ *paths:\n((?: *- \"[^\"]+\"\n)+)", re.M)


def _path_lists() -> list[list[str]]:
    blocks = _PATHS_BLOCK.findall(WORKFLOW.read_text(encoding="utf-8"))
    return [re.findall(r'- "([^"]+)"', block) for block in blocks]


def test_the_push_and_pull_request_filters_name_the_same_paths():
    """The failure this catches is one-sided and quiet: a path added to the push filter
    alone leaves the gate green on every pull request that changes it, which is the half
    that was supposed to catch things before they merged."""
    lists = _path_lists()
    assert len(lists) == 2, f"expected a push filter and a pull_request filter, got {len(lists)}"
    assert lists[0] == lists[1], f"path filters have drifted:\n{lists[0]}\n{lists[1]}"


def test_every_filtered_path_exists():
    """A filter naming a file that was renamed or deleted is a gate that quietly stops
    firing for it. Nothing else in the repo would report that."""
    missing = [p for p in _path_lists()[0] if not (ROOT / p).exists()]
    assert not missing, f"the workflow filters on paths that do not exist: {missing}"


def test_the_gate_covers_the_page_and_what_can_break_it_underneath():
    """The page is not the only input to the probe. app.py serves it and builds its CSP,
    manifest.py hashes the inline blocks, store.py owns the sweep and the note and nonce
    rules the signed sections ride on, and sign.py is what the browser's did:key derivation
    is checked against. Dropping any of them from the filter is how the gate goes green on
    the change that breaks the page."""
    filtered = set(_path_lists()[0])
    assert {
        "src/humans.html",
        "src/app.py",
        "src/manifest.py",
        "src/store.py",
        "scripts/sign.py",
        "tests/humans_ui_probe.mjs",
    } <= filtered


def _commands() -> str:
    """The workflow with its comment lines removed.

    Needed because this file and that one discuss the same commands in prose — the workflow
    explains why it runs `npm ci` *and not* `npm install`, and a naive substring check reads
    its own rationale as a violation.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_workflow_runs_the_probe_that_is_in_the_repo():
    assert "node tests/humans_ui_probe.mjs" in _commands()
    assert (ROOT / "tests" / "humans_ui_probe.mjs").exists()
    # `npm ci` and not `npm install`: the lockfile is the pin, exactly as uv.lock is, and
    # `npm install` would quietly resolve a newer Playwright than the one committed.
    assert "npm ci" in _commands() and "npm install" not in _commands()


def test_playwright_is_pinned_exactly_and_the_lockfile_agrees():
    """Everything else here is pinned — action SHAs, uv.lock, the Python version — and a
    range would make the browser gate the one place where CI silently changes underneath."""
    declared = json.loads(PACKAGE.read_text(encoding="utf-8"))["dependencies"]["playwright"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared), f"not an exact pin: {declared!r}"

    locked = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    assert locked["packages"]["node_modules/playwright"]["version"] == declared


def test_no_browser_dependency_reached_the_python_line():
    """The reason the browser gate is a separate workflow with its own npm install.

    The service and its suite are pure Python with three pinned packages, and `uv sync`
    must never see Node. Asserted against the files a dependency would actually land in,
    because "we agreed not to" is not a check.
    """
    for name in ("pyproject.toml", "uv.lock", "mcp/pyproject.toml"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert "playwright" not in text, f"{name} names playwright"

    # And the JS footprint stays where it was put: one package.json under tests/, never at
    # the repo root, where it would read as "this project is a Node project".
    assert not (ROOT / "package.json").exists()
    assert PACKAGE.exists() and LOCKFILE.exists()
