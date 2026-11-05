"""Run: uv run --group dev python -m pytest tests

`bench/rooms.py` reports the size of the store it measured (`rooms`, `deep`), so an
argument pair that builds a *different* store makes the whole run misleading rather than
merely slow — issue #988's case, `--deep 2 --rooms 1`, built two rooms and printed
`rooms: 1`.

Checked the way tests/unit/test_config_knobs.py checks a knob: a fresh interpreter, not
this one. `bench/rooms.py` is a script and not a package — it sets its own environment
and inserts `src` on the path at import — so importing it in-process would leave those
changes behind for whatever test runs next. The probe loads it by path in a subprocess
and reports what it built.
"""

import functools
import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2] / "bench" / "rooms.py"

# %r is the script's path. Each scenario gets its own tempdir so "what was left behind"
# is a real observation and not the previous scenario's rooms.
PROBE = """
import importlib.util, json, pathlib, sys, tempfile

spec = importlib.util.spec_from_file_location("bench_rooms", %r)
bench = importlib.util.module_from_spec(spec)
# Registered before it runs: the script's own `@dataclass` resolves its annotations
# through sys.modules[cls.__module__], which is None for a module that was never entered.
sys.modules["bench_rooms"] = bench
spec.loader.exec_module(bench)


def rooms(root):
    # rooms/<shard>/<name>.jsonl — the shard is part of the layout, so walk rather than
    # glob one level (a one-level glob finds nothing and reads as "no rooms built").
    d = root / "rooms"
    return sorted(d.rglob("*.jsonl")) if d.exists() else []


def shape(names):
    return {"rooms": len(names), "deep": sum(p.name.startswith("bench-d") for p in names)}


out = {}
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bench.build(root, 5, 2, 1)
    out["partial"] = shape(rooms(root))
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    bench.build(root, 3, 3, 1)
    out["all_deep"] = shape(rooms(root))
for label, args in (("deep_past_rooms", (1, 2, 1)), ("negative_deep", (5, -1, 1))):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        try:
            bench.build(root, *args)
        except ValueError as exc:
            out[label] = str(exc)
        else:
            out[label] = None
        out[label + "_left_behind"] = len(rooms(root))
print(json.dumps(out))
"""


@functools.cache
def probe() -> dict:
    """What `build` wrote, for every argument pair below, in one fresh interpreter."""
    run = subprocess.run([sys.executable, "-c", PROBE % str(BENCH)], capture_output=True, text=True)
    assert run.returncode == 0, f"probe failed: {run.stderr}"
    return json.loads(run.stdout)


def test_the_store_holds_one_room_per_argument_and_no_more() -> None:
    """The count in the report is the count on disk, which is the claim #988 broke:
    `deep` rooms were built *on top of* the shallow ones rather than out of them."""
    assert probe()["partial"] == {"rooms": 5, "deep": 2}


def test_deep_may_be_every_room() -> None:
    """`deep == rooms` is the boundary the invariant allows: a store with no shallow half
    at all, which is what a ladder reaching the full window asks for."""
    assert probe()["all_deep"] == {"rooms": 3, "deep": 3}


def test_deep_past_rooms_is_refused_by_name() -> None:
    """Refused, not clamped. Clamping the deep loop would quietly measure `rooms` rooms
    while the caller believes it asked for `deep` — the same misreport, one step later."""
    refused = probe()["deep_past_rooms"]
    assert refused is not None, "deep=2 with rooms=1 built a store anyway"
    assert "deep (2)" in refused and "rooms (1)" in refused


def test_a_negative_deep_is_refused_too() -> None:
    """The shallow loop is `range(rooms - deep)`, so a negative `deep` overflows in the
    other direction: more shallow rooms than `rooms`."""
    refused = probe()["negative_deep"]
    assert refused is not None, "deep=-1 with rooms=5 built a store anyway"
    assert "deep (-1)" in refused and "rooms (5)" in refused


def test_a_refused_build_writes_nothing() -> None:
    """The refusal comes before `mkdir`, so a run that cannot describe a store leaves no
    partial one behind for the next call to inherit."""
    values = probe()
    assert values["deep_past_rooms_left_behind"] == 0
    assert values["negative_deep_left_behind"] == 0


def test_the_cli_refuses_before_it_pays_for_the_setup() -> None:
    """`main` refuses the pair itself rather than letting `build` raise through it: the
    setup writes `depth` records for every deep room, so the check has to come first."""
    run = subprocess.run(
        [sys.executable, str(BENCH), "--rooms", "1", "--deep", "2"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 2, run.stderr
    assert "--deep 2" in run.stderr and "--rooms 1" in run.stderr
