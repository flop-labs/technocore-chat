"""Keep the room benchmark's ladder labels aligned with its measured windows."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import store

BENCH = Path(__file__).resolve().parents[2] / "bench" / "rooms.py"


@pytest.mark.parametrize("bad_limit", [-1, 0, store.MAX_LIMIT + 1])
def test_room_benchmark_refuses_clamped_ladder_points(bad_limit: int) -> None:
    """A clamped point must not be reported as if its requested windows were read."""
    result = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--rooms",
            "1",
            "--deep",
            "1",
            "--depth",
            "1",
            "--reps",
            "1",
            "--limits",
            str(bad_limit),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--limits" in result.stderr


def test_room_benchmark_keeps_valid_ladder_points() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(BENCH),
            "--rooms",
            "1",
            "--deep",
            "1",
            "--depth",
            "1",
            "--reps",
            "1",
            "--limits",
            "1",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["rows"][0]["limit"] == 1
