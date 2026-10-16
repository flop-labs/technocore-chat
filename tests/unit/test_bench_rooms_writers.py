"""Run: uv run --group dev python -m pytest tests

`--writers` names the size of the contended arm, so a run that starts fewer threads than it
reports — or appends to a room it never built — makes the column misleading rather than
merely slower. Issue #992's two cases: `--deep 0 --writers N` churned `bench-d000000`, a room
`build(rooms, deep=0, …)` never makes, and `--writers 8 --deep 2` started two threads while
`meta` still said eight.

Checked in a fresh interpreter, like tests/unit/test_config_knobs.py checks a knob:
`bench/rooms.py` is a script rather than a package — it sets its own environment and puts
`src` on the path at import — so importing it in-process would leave both behind for whatever
test runs next. The probe loads it by path in a subprocess and reports what it started and
what it left on disk.
"""

import functools
import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2] / "bench" / "rooms.py"

# %r is the script's path. `threading.Thread` is wrapped inside the probe so the arm's actual
# width is an observation instead of a re-reading of the `range` above it — the defect was
# exactly that the range and the report disagreed.
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


def run(root, **kw):
    started = []
    orig = bench.threading.Thread

    class Rec:
        def __init__(self, target=None, args=(), daemon=None):
            self._args = args
            self._t = orig(target=target, args=args, daemon=True)

        def start(self):
            started.append(self._args)
            self._t.start()

        def join(self, timeout=None):
            self._t.join(timeout)

    bench.threading.Thread = Rec
    error = None
    try:
        bench.contended(root, 1, 1, **kw)
    except ValueError as exc:
        error = str(exc)
    finally:
        bench.threading.Thread = orig
    return {"started": started, "error": error, "rooms": len(rooms(root))}


out = {}
for label, rooms_n, deep, kw in (
    ("deep_zero", 3, 0, {"writers": 1, "deep": 0}),
    ("wide_writers", 4, 2, {"writers": 8, "deep": 2}),
    ("every_deep_room", 4, 2, {"writers": 2, "deep": 2}),
    ("one_writer", 5, 3, {"writers": 1, "deep": 3}),
):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bench.build(root, rooms_n, deep, 2)
        before = len(rooms(root))
        result = run(root, **kw)
        result["built"] = before
        out[label] = result
print(json.dumps(out))
"""


@functools.cache
def probe() -> dict:
    """What `contended` started and left behind, for each argument set, in one fresh process."""
    run = subprocess.run([sys.executable, "-c", PROBE % str(BENCH)], capture_output=True, text=True)
    assert run.returncode == 0, f"probe failed: {run.stderr}"
    return json.loads(run.stdout)


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BENCH), *args], capture_output=True, text=True, timeout=120
    )


def test_a_writer_with_no_deep_room_is_refused() -> None:
    """`deep == 0` has no room to churn: the churn target is `bench-d{k}`, and none exist."""
    case = probe()["deep_zero"]
    assert case["error"] is not None, "deep=0 with a writer ran anyway"
    assert "writers (1)" in case["error"] and "deep (0)" in case["error"]


def test_a_refused_contended_arm_creates_no_room() -> None:
    """The room that used to appear here was `bench-d000000` — the newest room on disk, and
    so the head of the listing the arm was timing."""
    case = probe()["deep_zero"]
    assert case["rooms"] == case["built"], "the refused arm still wrote to the store"
    assert case["started"] == [], "the refused arm still started a thread"


def test_more_writers_than_deep_rooms_is_refused_by_name() -> None:
    """Refused, not clamped. Clamping to `--deep` measures a lighter arm than the one the
    meta and the `--json` payload report, which is the same misreport one step later."""
    case = probe()["wide_writers"]
    assert case["error"] is not None, "writers=8 with deep=2 ran anyway"
    assert "writers (8)" in case["error"] and "deep (2)" in case["error"]
    assert case["started"] == []


def test_the_arm_is_as_wide_as_it_is_named() -> None:
    """`writers == deep` is the boundary the invariant allows: one thread per deep room."""
    case = probe()["every_deep_room"]
    assert case["error"] is None, case["error"]
    assert len(case["started"]) == 2, case["started"]


def test_a_narrow_arm_starts_its_own_width() -> None:
    """The other direction: fewer writers than deep rooms, and none of them invented. The
    tuples come back as lists — the probe's answer crosses a JSON boundary."""
    case = probe()["one_writer"]
    assert case["error"] is None, case["error"]
    assert case["started"] == [[0]], case["started"]


def test_the_cli_refuses_a_writer_with_no_deep_room() -> None:
    """`main` refuses the pair itself rather than letting `contended` raise through it: the
    setup writes `depth` records per deep room, and this pair is wrong before any of it."""
    run = cli("--deep", "0", "--writers", "2", "--limits", "1", "--reps", "1")
    assert run.returncode == 2, run.stderr
    assert "--writers 2" in run.stderr and "--deep 0" in run.stderr


def test_the_cli_refuses_a_negative_writer_count_before_the_setup() -> None:
    """`-1` is truthy, so without its own check it reached `contended()` and raised there —
    after `build()` had written `depth` records per deep room. Exit 2 is `main()`'s refusal;
    a `ValueError` out of the arm would be an uncaught traceback and exit 1, so the code is
    what says the pair was refused before the setup rather than behind it."""
    run = cli("--rooms", "50", "--deep", "2", "--depth", "20", "--writers", "-1")
    assert run.returncode == 2, run.stderr
    assert "--writers -1" in run.stderr and "negative" in run.stderr


def test_the_cli_refuses_more_writers_than_deep_rooms() -> None:
    run = cli("--rooms", "4", "--deep", "2", "--writers", "8", "--limits", "1", "--reps", "1")
    assert run.returncode == 2, run.stderr
    assert "--writers 8" in run.stderr and "--deep 2" in run.stderr


def test_no_writers_is_still_a_run_and_not_a_refusal() -> None:
    """The refusal keys on a writer count, not on `--deep 0`: a store with no deep half is a
    shape the ladder can legitimately ask for, and the cold and warm arms still run."""
    run = cli("--rooms", "3", "--deep", "0", "--limits", "1", "--reps", "1")
    assert run.returncode == 0, run.stderr
    assert "contended" not in run.stdout


def test_the_report_names_the_arm_that_ran() -> None:
    """The positive half of the `--writers 8 --deep 2` case: a run that is allowed keeps
    reporting the width it actually started."""
    run = cli(
        "--rooms", "4", "--deep", "2", "--writers", "2", "--limits", "1", "--reps", "1", "--json"
    )
    assert run.returncode == 0, run.stderr
    meta = json.loads(run.stdout)["meta"]
    assert meta["writers"] == 2 and meta["deep"] == 2
