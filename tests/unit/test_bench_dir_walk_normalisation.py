"""bench/dir_walk.py must divide each group's time by the items that group visits.

`build()` makes orphan locks by unlinking half the data files, so the tree it hands to the
arms holds FILES locks and FILES/2 `.jsonl` files. The walk arms select `.jsonl`, the sweep
arms select `.jsonl.lock`, and a column labelled per file or per lock has to be normalised
to whichever of those the arm actually touched.

Observed rather than read out of the source: `per_file_us` and `build` are wrapped, `main()`
is run over a small tree, and the assertions are about the divisor each arm was given and
the counts the banner printed.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

WALK_ARMS = ("readdir_only", "path_stat", "direntry_stat", "os_stat_path", "os_stat_dir_fd")
SWEEP_ARMS = ("sweep_as_written", "sweep_os_stat", "sweep_os_access")


def _dir_walk(files: int):
    """A fresh bench/dir_walk.py, loaded by path the same way the benchmark runs.

    Fresh per test because FILES is lowered: `per_file_us`'s divisor is the default of its
    `n` parameter, bound when the module is defined, so the module has to be re-executed
    for a smaller tree to carry a matching default.
    """
    source = (ROOT / "bench" / "dir_walk.py").read_text(encoding="utf-8")
    source = source.replace("FILES = 10_000", f"FILES = {files}", 1)
    # 200_000 Path() constructions is the calibration line, and it is pure Python with no
    # bearing on any divisor. Timed once instead, so the test is seconds rather than minutes.
    source = source.replace("n: int = 200_000", "n: int = 200", 1)
    spec = importlib.util.spec_from_file_location("dir_walk", ROOT / "bench" / "dir_walk.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, str(ROOT / "bench" / "dir_walk.py"), "exec"), module.__dict__)
    assert module.FILES == files
    return module


@pytest.fixture
def run_main():
    """Run `main()` over a small tree, reporting what each arm was divided by.

    Returns (divisors, counts, output): the `n` every `per_file_us` call received keyed by
    arm name, the files and locks `build()` left on disk, and everything `main()` printed.
    """

    def go(capsys, files: int = 100):
        module = _dir_walk(files)
        divisors: dict[str, int] = {}
        counts: dict[str, int] = {}

        real_per_file_us = module.per_file_us
        real_build = module.build

        def recording_per_file_us(work, root, n=module.FILES):
            divisors[work.__name__] = n
            return real_per_file_us(work, root, n=n)

        def recording_build(root):
            real_build(root)
            names = os.listdir(root)
            counts["data"] = sum(1 for x in names if x.endswith(".jsonl"))
            counts["locks"] = sum(1 for x in names if x.endswith(".jsonl.lock"))

        module.per_file_us = recording_per_file_us
        module.build = recording_build
        module.main()
        return divisors, counts, capsys.readouterr().out

    return go


def test_build_leaves_half_the_data_files_and_every_lock():
    module = _dir_walk(100)
    root = tempfile.mkdtemp(prefix="dir-walk-")
    module.build(root)
    names = os.listdir(root)
    assert sum(1 for x in names if x.endswith(".jsonl.lock")) == 100
    assert sum(1 for x in names if x.endswith(".jsonl")) == 50
    assert module.readdir_only(root) == 50, "readdir_only is the walk arms' own selector"


def test_the_walk_arms_are_divided_by_the_data_files_they_visit(capsys, run_main):
    divisors, counts, _ = run_main(capsys)
    assert counts["data"] * 2 == counts["locks"]
    for arm in WALK_ARMS:
        assert divisors[arm] == counts["data"], (
            f"{arm} visits the {counts['data']} surviving data files but its us/file was "
            f"divided by {divisors[arm]}"
        )


def test_the_sweep_arms_stay_divided_by_every_lock(capsys, run_main):
    """The control: the sweep group was already right and must not be moved."""
    divisors, counts, _ = run_main(capsys)
    for arm in SWEEP_ARMS:
        assert divisors[arm] == counts["locks"], (
            f"{arm} inspects every one of the {counts['locks']} locks"
        )


def test_the_banner_states_the_data_file_count_that_is_on_disk(capsys, run_main):
    _, counts, out = run_main(capsys)
    banner = next(line for line in out.splitlines() if "data files +" in line)
    assert banner.startswith(f"{counts['data']} data files + {counts['locks']} lock sidecars"), (
        f"banner reads {banner.strip()!r}; the tree holds {counts['data']} data files and "
        f"{counts['locks']} lock sidecars"
    )
