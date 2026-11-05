"""An incomplete live document must not bypass its complete outage snapshot."""

import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.parametrize(
    "mode",
    [
        "headers-reset",
        "body-reset",
        "body-timeout",
        "missing-copy",
        "healthy",
        "empty",
        "head",
        "refusal",
        "not-modified",
        "unlisted",
    ],
)
def test_document_fallback_covers_the_complete_origin_body(mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to execute the edge Worker")
    here = pathlib.Path(__file__).resolve().parent
    run = subprocess.run(
        [
            node,
            str(here / "document_body_probe.mjs"),
            str(here.parents[1] / "edge/src/worker.js"),
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode == 0, run.stdout + run.stderr
