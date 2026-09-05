"""The mutation run's copied tree has to hold what the suite it runs there reads.

Run: uv run --group dev python -m pytest tests
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The two forms the suite uses to reach out of tests/ and into the repo root.
_ROOT_READ = re.compile(r'(?:ROOT|parents\[2\])\s*/\s*"([^"/]+)"')

# mutmut builds these into the copy itself: the mutated sources, the suite it selects from,
# and the config it re-reads inside the tree.
_ALWAYS_PRESENT = frozenset({"src", "tests", "pyproject.toml"})


def test_every_root_path_the_mutation_suite_reads_is_copied_into_the_tree():
    """mutmut runs the suite in a copy of the project, so anything a selected test reads
    from the repo root has to be in `also_copy` — the rule stated in the comment above it.

    It stopped holding. README.md (tests/http/test_rooms.py) and scripts/ (test_signer.py)
    both arrived after `also_copy` was last touched, and the weekly run has been dying in
    its own baseline — FileNotFoundError on mutants/README.md — before generating a single
    mutant. A scoped mutation suite that cannot start is worse than not having one: the
    report is empty either way, and only one of them looks like it ran.
    """
    with (ROOT / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)["tool"]["mutmut"]
    copied = set(cfg["also_copy"]) | _ALWAYS_PRESENT
    selection = cfg["pytest_add_cli_args_test_selection"]
    ignored = {arg.split("=", 1)[1] for arg in selection if arg.startswith("--ignore=")}

    missing: dict[str, set[str]] = {}
    for entry in (t for t in selection if not t.startswith("-")):
        target = ROOT / entry
        for f in sorted(target.rglob("*.py")) if target.is_dir() else [target]:
            if str(f.relative_to(ROOT)) in ignored:
                continue
            for name in _ROOT_READ.findall(f.read_text(encoding="utf-8")):
                if name not in copied:
                    missing.setdefault(name, set()).add(str(f.relative_to(ROOT)))

    assert not missing, (
        "read from the repo root by the mutation suite, but absent from "
        f"[tool.mutmut] also_copy: { ({k: sorted(v) for k, v in missing.items()}) }"
    )
