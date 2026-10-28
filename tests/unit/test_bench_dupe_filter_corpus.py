"""bench/dupe_filter.py must describe the corpus it built, at every copy threshold.

`CHOSEN` reads `config.DUPE_MAX_COPIES`, and the module invites being run under another
value. `build_corpus` sizes the borderline class from a row budget, so its group count
shrinks as the threshold rises. The summary line `main()` prints has to follow it.

The line is located in `main()`'s AST and evaluated against the module's globals, so what
is asserted is the string the benchmark prints rather than a restatement of it. Running
`main()` for real would mean five passes over seven parameter sets plus the window sweep,
and the line is written before any of that.
"""

from __future__ import annotations

import ast
import importlib.util
import random
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "bench" / "dupe_filter.py"


def _dupe_filter(monkeypatch: pytest.MonkeyPatch, max_copies: int):
    """bench/dupe_filter.py under CHAT_DUPE_MAX_COPIES=<max_copies>.

    config reads the environment at import and CHOSEN reads config at import, so config,
    limit and the benchmark are all re-executed per threshold.
    """
    monkeypatch.setenv("CHAT_DUPE_MAX_COPIES", str(max_copies))
    for name in ("config", "limit"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(f"dupe_filter_{max_copies}", BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.CHOSEN[1] == max_copies
    return module


def _summary_line(module) -> str:
    tree = ast.parse(BENCH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and len(node.args) == 1
            and "borderline x" in ast.unparse(node.args[0])
        ):
            return eval(  # noqa: S307 - the expression is this module's own print argument
                compile(ast.Expression(node.args[0]), str(BENCH), "eval"),
                dict(module.__dict__),
            )
    raise AssertionError("no print(...) carrying 'borderline x' in bench/dupe_filter.py")


def _built(module) -> tuple[int, int]:
    """The borderline class as built: (groups, copies per group)."""
    rows = module.build_corpus(random.Random(20260827))
    texts = Counter(text for text, label in rows if label == "borderline")
    assert texts, "the borderline class must exist at every threshold"
    assert len(set(texts.values())) == 1, texts
    return len(texts), next(iter(texts.values()))


# 5 is the shipped default; the others are the kind of value the module's own comment
# invites (`CHAT_DUPE_MAX_COPIES=8 uv run python bench/dupe_filter.py`) and each lands on a
# different floor of the row budget.
@pytest.mark.parametrize("max_copies", [5, 8, 11, 15])
def test_the_summary_describes_the_borderline_class_it_built(monkeypatch, max_copies):
    module = _dupe_filter(monkeypatch, max_copies)
    groups, copies = _built(module)
    found = re.search(r"(\d+) borderline x(\d+)", _summary_line(module))
    assert found, _summary_line(module)
    assert (int(found.group(1)), int(found.group(2))) == (groups, copies), (
        f"at CHAT_DUPE_MAX_COPIES={max_copies} the line claims "
        f"{found.group(1)} x {found.group(2)} borderline rows, the corpus holds "
        f"{groups} x {copies}"
    )


@pytest.mark.parametrize("max_copies", [5, 8, 11, 15])
def test_the_borderline_class_stays_inside_its_row_budget(monkeypatch, max_copies):
    """The control: the row budget is what makes the group count vary in the first place."""
    module = _dupe_filter(monkeypatch, max_copies)
    groups, copies = _built(module)
    assert copies == max_copies + 1, "one copy past the threshold in force"
    assert groups * copies <= module.BORDERLINE_ROWS
