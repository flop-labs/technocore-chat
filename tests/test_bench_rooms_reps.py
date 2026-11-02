"""Reject benchmark timing runs that cannot produce a sample."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench" / "rooms.py"


def run_benchmark(reps: int) -> subprocess.CompletedProcess[str]:
    """Use the real CLI with a tiny store so refusal and success are observable."""
    return subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--rooms",
            "1",
            "--deep",
            "1",
            "--depth",
            "1",
            "--limits",
            "1",
            "--reps",
            str(reps),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("reps", [0, -1])
def test_room_benchmark_refuses_empty_samples_before_setup(reps: int) -> None:
    result = run_benchmark(reps)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--reps must be at least 1" in result.stderr
    assert "Traceback" not in result.stderr


def test_room_benchmark_still_measures_one_sample() -> None:
    result = run_benchmark(1)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["meta"]["reps"] == 1
    assert payload["rows"][0]["limit"] == 1
    assert payload["rows"][0]["cold"]["median"] >= 0
    assert payload["rows"][0]["warm"]["median"] >= 0
