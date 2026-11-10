"""Run: uv run --group dev python -m pytest tests

`bench/rooms.py` builds its deep rooms one record at a time, and a room exists only once
it holds a record — so `--depth 0` (or any depth below 1) with `--deep 2` built the shallow
half and no deep rooms at all, then reported `deep: 2` over a store that had none. The
windows the benchmark is about were never read.

Checked the way tests/unit/test_config_knobs.py checks a knob: a fresh interpreter, not
this one. `bench/rooms.py` is a script and not a package — it sets its own environment and
inserts `src` on the path at import — so importing it in-process would leave those changes
behind for whatever test runs next. The probe loads it by path and reports what it built.
"""

import functools
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2] / "bench" / "rooms.py"

# The script reads CHAT_* at import (CHAT_MAX_ROOMS gates room creation), so an operator's
# environment would decide what these probes build. Stripped, as test_config_knobs.py does.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}

# %r is the script's path. Each scenario gets its own tempdir so "what was left behind" is
# a real observation and not the previous scenario's rooms.
PROBE = """
import importlib.util, json, pathlib, sys, tempfile

sys.argv = [sys.argv[0]]
spec = importlib.util.spec_from_file_location("bench_rooms", %r)
bench = importlib.util.module_from_spec(spec)
# Registered before it runs: the script's own `@dataclass` resolves its annotations
# through sys.modules[cls.__module__], which is None for a module that was never entered.
sys.modules["bench_rooms"] = bench
spec.loader.exec_module(bench)


def shape(root):
    # rooms/<shard>/<name>.jsonl — the shard is part of the layout, so walk rather than
    # glob one level (a one-level glob finds nothing and reads as "no rooms built").
    d = root / "rooms"
    files = sorted(d.rglob("*.jsonl")) if d.exists() else []
    return {
        "made_dir": d.exists(),
        "shallow": sorted(len(p.read_text().splitlines()) for p in files if p.stem.startswith("bench-s")),
        "deep": sorted(len(p.read_text().splitlines()) for p in files if p.stem.startswith("bench-d")),
    }


out = {}
for label, args in (
    ("depth_0", (4, 2, 0)),
    ("depth_negative", (4, 2, -1)),
    ("depth_1", (4, 2, 1)),
    ("depth_3", (4, 2, 3)),
    ("shallow_only_depth_0", (4, 0, 0)),
    ("shallow_only_depth_negative", (4, 0, -1)),
):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        try:
            bench.build(root, *args)
        except ValueError as exc:
            out[label] = {"refused": str(exc), **shape(root)}
        else:
            out[label] = {"refused": None, **shape(root)}
print(json.dumps(out))
"""

# The CLI half: does main() refuse before it creates the temporary store? An exit status of
# 2 says argparse refused, but not *when* — so the probe swaps TemporaryDirectory and build
# for recorders and reports whether either was reached.
CLI_PROBE = """
import importlib.util, json, sys

argv = json.loads(sys.argv[1])
sys.argv = ["rooms.py", *argv]
spec = importlib.util.spec_from_file_location("bench_rooms", %r)
bench = importlib.util.module_from_spec(spec)
sys.modules["bench_rooms"] = bench
spec.loader.exec_module(bench)

reached = []


class Recorder:
    def __init__(self, *a, **k):
        reached.append("tempdir")
        raise SystemExit(99)


bench.tempfile.TemporaryDirectory = Recorder
try:
    bench.main()
    code = 0
except SystemExit as exc:
    code = exc.code
print(json.dumps({"code": code, "reached": reached}))
"""


@functools.cache
def probe() -> dict:
    """What `build` wrote, for every argument triple below, in one fresh interpreter."""
    run = subprocess.run(
        [sys.executable, "-c", PROBE % str(BENCH)], capture_output=True, text=True, env=ENV
    )
    assert run.returncode == 0, f"probe failed: {run.stderr}"
    return json.loads(run.stdout)


def cli(*argv: str) -> tuple[dict, str]:
    run = subprocess.run(
        [sys.executable, "-c", CLI_PROBE % str(BENCH), json.dumps(argv)],
        capture_output=True,
        text=True,
        env=ENV,
    )
    assert run.stdout.strip(), f"cli probe printed nothing: {run.stderr}"
    return json.loads(run.stdout.strip().splitlines()[-1]), run.stderr


def test_depth_below_one_with_deep_rooms_is_refused_by_name() -> None:
    """Refused, not clamped: a clamp to 1 would build deep rooms the caller did not ask
    for, and building none is the misreport this guards."""
    for label in ("depth_0", "depth_negative"):
        refused = probe()[label]["refused"]
        assert refused is not None, f"{label}: build() made a store with no deep rooms"
        assert "depth" in refused


def test_a_refused_build_writes_nothing() -> None:
    """The refusal comes before `mkdir`, so a call that cannot build the store it names
    leaves no partial one behind — not even the shallow half."""
    for label in ("depth_0", "depth_negative"):
        assert probe()[label]["made_dir"] is False, label


def test_depth_one_builds_both_halves() -> None:
    """The smallest valid deep store: two shallow rooms and two deep ones of one record."""
    assert probe()["depth_1"] == {
        "refused": None,
        "made_dir": True,
        "shallow": [1, 1],
        "deep": [1, 1],
    }


def test_deeper_rooms_hold_depth_records() -> None:
    assert probe()["depth_3"]["deep"] == [3, 3]


def test_a_shallow_only_store_ignores_depth() -> None:
    """`depth` is only read by the deep loop, so with `deep == 0` it says nothing about the
    store and any value is fine — refusing it would break a shallow-only run for no reason."""
    for label in ("shallow_only_depth_0", "shallow_only_depth_negative"):
        assert probe()[label] == {
            "refused": None,
            "made_dir": True,
            "shallow": [1, 1, 1, 1],
            "deep": [],
        }


def test_the_cli_refuses_before_the_temporary_store_exists() -> None:
    for depth in ("0", "-1"):
        result, stderr = cli("--rooms", "4", "--deep", "2", "--depth", depth)
        assert result == {"code": 2, "reached": []}, stderr
        assert f"--depth {depth}" in stderr
        assert "--deep 2" in stderr
        assert "--depth must be >= 1 when --deep > 0" in stderr


def test_the_cli_reaches_setup_for_a_valid_pair() -> None:
    """The control for the test above: the recorder does fire when the arguments are fine,
    so an empty `reached` there means refused, not unobserved."""
    for argv in (("--deep", "2", "--depth", "1"), ("--deep", "0", "--depth", "0")):
        result, stderr = cli("--rooms", "4", *argv)
        assert result == {"code": 99, "reached": ["tempdir"]}, stderr
