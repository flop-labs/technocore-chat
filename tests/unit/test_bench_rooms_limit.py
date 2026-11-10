"""Keep the room benchmark's ladder labels aligned with its measured windows."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
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


def test_room_benchmark_is_staged_for_mutation() -> None:
    """The weekly mutmut workspace must retain the CLI used by these tests."""
    project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    config = tomllib.loads(project.read_text(encoding="utf-8"))
    assert "bench" in config["tool"]["mutmut"]["also_copy"]


@pytest.mark.parametrize("limit", [1, store.MAX_LIMIT])
def test_room_benchmark_accepts_both_range_endpoints(limit: int) -> None:
    """Both inclusive endpoints must be measured without a clamp."""
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
            str(limit),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["rows"][0]["limit"] == limit


def test_room_benchmark_rejects_mixed_valid_and_invalid_ladder() -> None:
    """One invalid point must reject the entire ladder before measurement."""
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
            "5",
            "0",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--limits" in result.stderr
