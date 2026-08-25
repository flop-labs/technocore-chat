"""The mutation run happens in a copy of the project, and a missing file costs the whole run.

mutmut rebuilds the tree under `mutants/` and runs the suite there. It brings `pyproject.toml`,
`uv.lock`, `tests/` and `source_paths` on its own; everything else the suite opens by path has to
be named in `also_copy`. Leave one out and the clean-tests pass fails inside the copy, mutmut
reports "failed to collect stats", and *no mutant is judged* — a weekly job that looks like it ran
and measured nothing. That is worth a cheap drift check: it is the kind of break nobody notices,
because the thing that would have told them is the thing that broke.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# mutmut copies these itself, so they are legitimately absent from `also_copy`.
ALWAYS_COPIED = {"pyproject.toml", "uv.lock", "tests"}

# The two idioms the suite uses to reach the repository root: an inline parents[2] / <name>, and
# a module-level ROOT = Path(__file__).resolve().parents[2] used as ROOT / <name>. Written without
# quoting either example on purpose — this pattern is run over the file it lives in, so a quoted
# one here would be scanned as if the suite really read a path called "name".
ROOT_READ = re.compile(r'(?:parents\[2\]|\bROOT)\s*/\s*"([^"]+)"')


def _mutmut_config() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]


def _selected_test_files() -> list[Path]:
    """The test modules the mutation run actually collects — selection minus --ignore."""
    args = _mutmut_config()["pytest_add_cli_args_test_selection"]
    ignored = {ROOT / a.split("=", 1)[1] for a in args if a.startswith("--ignore=")}
    files: list[Path] = []
    for target in (ROOT / a for a in args if not a.startswith("-")):
        found = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        files.extend(p for p in found if p not in ignored)
    return files


def test_every_repo_root_path_the_mutation_run_reads_is_copied_into_the_copy() -> None:
    config = _mutmut_config()
    available = set(config["also_copy"]) | set(config["source_paths"]) | ALWAYS_COPIED

    missing: dict[str, list[str]] = {}
    for path in _selected_test_files():
        for name in ROOT_READ.findall(path.read_text(encoding="utf-8")):
            if name not in available:
                missing.setdefault(name, []).append(path.relative_to(ROOT).as_posix())

    assert not missing, (
        "these repo-root paths are read by tests the mutation run collects, but never reach "
        f"mutants/: {missing}. Add them to [tool.mutmut] also_copy in pyproject.toml, or "
        "--ignore the test if it cannot be true of a mutated source tree."
    )


def test_everything_named_in_also_copy_still_exists() -> None:
    """The other direction: a path renamed in the repo and not here copies nothing, silently."""
    gone = [name for name in _mutmut_config()["also_copy"] if not (ROOT / name).exists()]
    assert not gone, f"also_copy names paths that no longer exist: {gone}"
