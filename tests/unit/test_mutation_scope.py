"""The mutation report's own failure mode: a run that judged nothing.

`report()` renders the weekly job's summary and picks its exit code. Both readings come
from counters that a runner which died before its first verdict leaves at zero — so
"every mutant was caught" and "no mutant was ever judged" arrive as the same numbers, and
only `total` tells them apart. `_survivors()` already refuses that confusion one level
down ("nothing survived" versus "the tool that lists survivors did not run"); these pin
the same distinction one level up, where the summary a human actually reads is written.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    # By path, like the store-doc generator: tests/ is not a package, and a bare pytest
    # invocation should not need it on the module search path.
    spec = importlib.util.spec_from_file_location(
        "mutation_scope", ROOT / "tests" / "mutation_scope.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(monkeypatch, tmp_path, capsys, **counters) -> tuple[int, str]:
    """Run `report()` over one stats file and return (exit code, rendered markdown)."""
    module = _load()
    stats = {
        "killed": 0,
        "survived": 0,
        "total": 0,
        "no_tests": 0,
        "skipped": 0,
        "suspicious": 0,
        "timeout": 0,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
        **counters,
    }
    path = tmp_path / "mutmut-cicd-stats.json"
    path.write_text(json.dumps(stats), encoding="utf-8")
    monkeypatch.setattr(module, "STATS", path)
    # Stubbed: the real one shells out to `mutmut results`, which needs a run on disk.
    monkeypatch.setattr(module, "_survivors", lambda: [])
    code = module.report()
    return code, capsys.readouterr().out


def test_a_run_that_judged_nothing_is_broken_not_a_clean_sweep(monkeypatch, tmp_path, capsys):
    """mutmut generating mutants and judging none is the harness breaking.

    It is what a baseline failing inside `mutants/` produces — a path the tests read from
    the repo root that `also_copy` does not carry — and it leaves every counter in BROKEN
    at zero. The workflow's contract is that this exits non-zero ("mutants generated and
    never judged"), and the summary must not claim the suite caught anything.
    """
    code, out = _report(monkeypatch, tmp_path, capsys, total=4139)

    assert code == 1, "a run that judged nothing must fail the job"
    assert "every mutant in scope was caught" not in out
    assert "The run itself is broken" in out
    assert "never_judged" in out


def test_a_real_clean_sweep_still_reads_as_one(monkeypatch, tmp_path, capsys):
    """The other side of the same boundary, so the fix cannot swallow a genuine pass."""
    code, out = _report(monkeypatch, tmp_path, capsys, killed=970, total=4139)

    assert code == 0
    assert "Nothing survived: every mutant in scope was caught by a test." in out
    assert "The run itself is broken" not in out
