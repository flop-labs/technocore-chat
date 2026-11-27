"""bench/dir_walk.py must divide each walk arm by the files that arm actually visits.

`build()` unlinks every other data file to leave a half-orphaned lock tree, so the walk
arms visit `FILES // 2` `.jsonl` files while the sweep arms inspect all `FILES` locks.
A single shared default divisor would report the walk's `us/file` at half its real cost,
and the banner would claim twice the data files that are on disk.

The test drives `main()` with the timing and tree-building replaced by recorders, so the
assertion is about the divisor each arm is handed rather than a noisy measurement.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location("bench_dir_walk", ROOT / "bench" / "dir_walk.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WALK_ARMS = {
    "readdir_only",
    "path_stat",
    "direntry_stat",
    "os_stat_path",
    "os_stat_dir_fd",
}


def test_walk_arms_divide_by_the_data_files_they_visit(monkeypatch, capsys):
    module = _module()
    recorded: list[tuple[str, int | None]] = []

    def recorder(work, root, n=None):
        recorded.append((work.__name__, n))
        return 1.0

    monkeypatch.setattr(module, "build", lambda root: None)
    monkeypatch.setattr(module, "per_file_us", recorder)
    monkeypatch.setattr(module, "per_call_us", lambda work, n=200_000: 0.0)

    module.main()
    out = capsys.readouterr().out

    data_files = module.FILES // 2
    walk_divisors = [n for name, n in recorded if name in WALK_ARMS]
    sweep_divisors = [n for name, n in recorded if name.startswith("sweep_")]

    assert walk_divisors == [data_files] * len(WALK_ARMS), (
        "every walk arm must divide by the .jsonl files it visits, not the lock count"
    )
    assert sweep_divisors == [None] * 3, "sweep arms keep the lock-count default"
    assert f"{data_files} data files + {module.FILES} lock sidecars" in out, (
        "the banner must state the data files that are on disk"
    )
