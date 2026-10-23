"""Regression test for #581: RESERVED_ROOM_BYTES must not silently drop to zero."""

import os
import subprocess
import sys


def test_reserved_room_bytes_zero_refuses_to_boot():
    """CHAT_MAX_ROOMS large enough to make RESERVED_ROOM_BYTES zero must not boot."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    run = subprocess.run(
        [sys.executable, "-c", "import store"],
        capture_output=True,
        text=True,
        env={**clean, "CHAT_MAX_ROOMS": "10000000000"},
        cwd=os.path.join(os.path.dirname(__file__), "..", "..", "src"),
    )
    assert run.returncode != 0, "app booted with RESERVED_ROOM_BYTES == 0"
    assert "ValueError" in run.stderr
