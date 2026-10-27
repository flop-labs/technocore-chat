"""A partial write workload must fail, not publish requested writes as throughput.

Fresh interpreters keep the benchmarks' store imports and injected failures out of the
service suite. Every real write targets a temporary store owned by the benchmark.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def probe(code: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "failure",
    [
        'OSError("injected append worker failure")',
        'SystemExit("injected append worker failure")',
        "SystemExit(0)",
    ],
)
def test_append_worker_failure_reaches_main_after_the_others_join(
    tmp_path: Path, failure: str
) -> None:
    finished = tmp_path / "finished"
    run = probe(
        f"""
import runpy, sys, threading
from pathlib import Path

bench = runpy.run_path("bench/append.py")
original = bench["store"].append
roles, calls = {{}}, {{}}
guard = threading.Lock()

def append(*args, **kwargs):
    thread = threading.current_thread()
    if thread is threading.main_thread():
        return original(*args, **kwargs)
    with guard:
        role = roles.setdefault(thread, len(roles))
        i = calls.get(thread, 0)
        calls[thread] = i + 1
    if role == 0 and i == 1:
        raise {failure}
    result = original(*args, **kwargs)
    if role == 1 and i == 2:
        Path({str(finished)!r}).write_text("finished")
    return result

bench["store"].append = append
sys.argv = [sys.argv[0], "--preload", "1", "--warm", "0", "--n", "2",
            "--threads", "2", "--per-thread", "3"]
bench["main"]()
"""
    )
    assert run.returncode != 0, run.stdout
    assert "benchmark worker failed" in run.stderr
    assert "appends/s" not in run.stdout
    assert finished.read_text() == "finished"


@pytest.mark.parametrize("arm", ["none", "batched"])
@pytest.mark.parametrize("failure", ['OSError("injected counter worker failure")', "SystemExit(0)"])
def test_counter_worker_failure_is_not_a_successful_process_report(
    tmp_path: Path, arm: str, failure: str
) -> None:
    finished = tmp_path / "finished"
    late_finished = tmp_path / "late-finished"
    # Patch the append in the real child interpreter, not the parent seeding rooms.
    # The no-counter arm is important: its exactness check cannot notice lost writes.
    injection = f"""
_original_append = store.append
def _append(*args, **kwargs):
    if args[3] == "m0-0-1":
        raise {failure}
    if args[3].startswith("m1-"):
        time.sleep(0.1)
        assert args[0].exists(), "store removed before the remaining process finished"
    result = _original_append(*args, **kwargs)
    if args[3] == "m0-1-2":
        store.Path({str(finished)!r}).write_text("finished")
    if args[3] == "m1-1-2":
        store.Path({str(late_finished)!r}).write_text("finished")
    return result
store.append = _append
"""
    run = probe(
        f"""
import runpy
bench = runpy.run_path("bench/counters.py")
worker_globals = bench["_round"].__globals__
worker_globals["WORKER"] = worker_globals["WORKER"].replace(
    "\\nimport store\\n", "\\nimport store\\n" + {injection!r}
)
bench["run"]([{arm!r}], ["spread"], 2, 2, 3, 1, False, "0", 0)
"""
    )
    assert run.returncode != 0, run.stdout
    assert "benchmark worker failed" in run.stderr
    assert "worker failed" in run.stderr
    assert "n/a (no counter)" not in run.stdout
    assert "exact in" not in run.stdout
    assert "store removed" not in run.stderr
    assert finished.read_text() == "finished"
    assert late_finished.read_text() == "finished"


def test_append_success_still_reports_the_completed_workload() -> None:
    run = probe(
        "import runpy, sys; "
        "sys.argv = ['append.py', '--preload', '1', '--warm', '0', '--n', '2', "
        "'--threads', '2', '--per-thread', '3']; "
        "runpy.run_path('bench/append.py', run_name='__main__')"
    )
    assert run.returncode == 0, run.stderr
    assert "2-thread p50" in run.stdout
    assert "appends/s" in run.stdout


def test_counter_success_still_reports_both_counter_arms() -> None:
    run = probe(
        "import runpy, sys; "
        "sys.argv = ['counters.py', '--procs', '1', '--threads', '2', '--writes', '3', "
        "'--rounds', '1', '--mixes', 'spread', '--arms', 'none,batched', '--fsync', '0']; "
        "runpy.run_path('bench/counters.py', run_name='__main__')"
    )
    assert run.returncode == 0, run.stderr
    assert "n/a (no counter)" in run.stdout
    assert "exact in 1/1" in run.stdout
    assert "WRONG" not in run.stdout
