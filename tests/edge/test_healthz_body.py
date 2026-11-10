"""The liveness deadline covers the complete origin response, including its body."""

import pathlib
import shutil
import subprocess

import pytest

EDGE = pathlib.Path(__file__).resolve().parents[2] / "edge"


def test_healthz_body_read_is_inside_the_origin_failure_guard():
    worker = (EDGE / "src" / "worker.js").read_text(encoding="utf-8")
    lane = worker.split("async function edgeCached(", 1)[1].split("/** Resolves", 1)[0]
    guarded = lane.split("try {", 1)[1].split("} catch", 1)[0]
    assert "await fresh.arrayBuffer()" in guarded, (
        "a body timeout must produce the bounded health failure, not an unbounded retry"
    )


def test_healthz_origin_failures_are_bounded_and_never_retried():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the executable Worker probe")
    subprocess.run(
        [
            node,
            str(pathlib.Path(__file__).with_name("healthz_body_probe.mjs")),
            str(EDGE / "src" / "worker.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
