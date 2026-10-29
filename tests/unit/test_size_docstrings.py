"""Docstrings are free prose, not a way to hide executable tokens on their line."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("size_measure", ROOT / "sz.py")
assert spec is not None and spec.loader is not None
sz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sz)


@pytest.mark.parametrize(
    ("source", "code_lines"),
    [
        ('"doc"; x = 1\n', 1),
        ('"doc\u2028part"; x = 1\n', 1),
        ('def f(): "doc"; return 1\n', 1),
        ('class C: "doc"; x = 1\n', 1),
        ('async def f(): "doc"; return 1\n', 1),
        ('def f():\n    """first\n    last"""; return 1\n', 2),
        ('def café(): "説明"; return 1\n', 1),
        ('("one"\n "two"); x = 1\n', 1),
        ('"doc"\n', 0),
        ('("one"\n "two")\n', 0),
        ('"doc";\n', 0),
        ('"doc" ; # comment\n', 0),
        ('("doc") ; # comment\n', 0),
        ('"""first\nlast""" ;\n', 0),
        ('"doc" \\\n;\n', 0),
        ('def f():\n    "doc"\n    return 1\n', 2),
        ('x = "not a docstring"\n', 1),
    ],
)
def test_only_the_docstring_tokens_are_excluded(tmp_path, source, code_lines):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    assert sz.measure(path)["code_lines"] == code_lines


@pytest.mark.parametrize("separator", [";", " ;"])
def test_inline_definition_keeps_its_non_docstring_tokens(tmp_path, separator):
    path = tmp_path / "sample.py"
    path.write_text(f'def café(): "説明"{separator} return 1\n', encoding="utf-8")
    assert sz.measure(path)["tokens_per_line"] == 7.0


@pytest.mark.parametrize("mode", ["--caps", "--check"])
def test_inline_docstrings_cannot_hide_growth_from_the_cli(tmp_path, mode):
    # An isolated policy fixture, never the maintainer-owned repository baseline.
    (tmp_path / "sz.py").write_bytes((ROOT / "sz.py").read_bytes())
    for name in (*sz.CORE_FILES, *sz.EXTRA_FILES):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("", encoding="utf-8")
    (tmp_path / sz.CORE_FILES[0]).write_text('"doc"; x = 1\n', encoding="utf-8")
    labels = ["core/" + Path(name).name for name in sz.CORE_FILES]
    baseline = {
        "core_total": 0,
        "caps": dict.fromkeys([*labels, "core_total"], 0),
        "files": {label: {"code_lines": 0} for label in labels},
    }
    (tmp_path / "sz-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "sz.py"), mode],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout
    assert "core/app.py" in result.stderr
