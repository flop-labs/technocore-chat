# SPDX-License-Identifier: Apache-2.0
"""Counter benchmark labels must name the workload that actually runs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path: Path, *options: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHAT_")}
    env.update(TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "bench/counters.py"),
            "--procs",
            "1",
            "--threads",
            "1",
            "--writes",
            "2",
            "--rounds",
            "1",
            "--fsync",
            "0",
            *options,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("arms", "mixes", "error"),
    [
        ("blockng", "lobby", "unknown --arms name: 'blockng'"),
        ("batched", "loby", "unknown --mixes name: 'loby'"),
        ("blocking,blockng", "lobby", "unknown --arms name: 'blockng'"),
        ("batched", "lobby,loby", "unknown --mixes name: 'loby'"),
        ("every", "spread", "invalid --arms interval: 'every'"),
        ("everybad", "spread", "invalid --arms interval: 'everybad'"),
        # float() accepts these, the interval does not: the arm would never flush.
        ("everynan", "spread", "invalid --arms interval: 'everynan'"),
        ("everyinf", "spread", "invalid --arms interval: 'everyinf'"),
        ("everyNaN", "spread", "invalid --arms interval: 'everyNaN'"),
        ("every-inf", "spread", "invalid --arms interval: 'every-inf'"),
        ("everyInfinity", "spread", "invalid --arms interval: 'everyInfinity'"),
        ("every50,everynan", "spread", "invalid --arms interval: 'everynan'"),
    ],
)
def test_invalid_selection_refuses_the_whole_run(tmp_path, arms, mixes, error):
    result = _run(tmp_path, "--arms", arms, "--mixes", mixes)
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""  # Even a valid prefix must not publish a partial result.
    assert error in result.stderr
    assert "Traceback" not in result.stderr


def test_valid_static_and_interval_arms_still_run_all_mixes(tmp_path):
    arms = [
        "blocking",
        "oppo",
        "batched",
        "none",
        "deferred",
        "every50",
        "every0",  # A zero interval flushes on every bump, so it stays valid.
        "every0.5",
        "every1e2",
        "every+50",
        "every1_000",
    ]
    result = _run(tmp_path, "--arms", ",".join(arms), "--mixes", "lobby,mixed,spread")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert result.stdout.count("exact in 1/1") == 30
    assert result.stdout.count("no counter") == 3
    for mix in ("lobby", "mixed", "spread"):
        assert f"--- {mix}\n" in result.stdout
    for arm in arms:
        assert sum(line.startswith(f"{arm:<10}") for line in result.stdout.splitlines()) == 3
