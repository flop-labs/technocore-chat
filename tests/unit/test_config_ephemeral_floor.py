"""Run: uv run --group dev python -m pytest tests"""

import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _boot(**env: str) -> int:
    """Boot a fresh interpreter to read the config value."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    probe = f"import sys; sys.path.insert(0, {SRC!r}); import config; print(config.EPHEMERAL_TTL_SECONDS)"
    run = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env={**clean, **env}
    )
    assert run.returncode == 0, f"boot failed: {run.stderr}"
    return int(run.stdout.strip())


def test_ephemeral_ttl_floors_at_minimum() -> None:
    """EPHEMERAL_TTL_SECONDS must floor at 60 to prevent negative/zero instant expiry."""
    assert _boot() == 900
    assert _boot(CHAT_EPHEMERAL_TTL_SECONDS="0") == 60
    assert _boot(CHAT_EPHEMERAL_TTL_SECONDS="-100") == 60
    assert _boot(CHAT_EPHEMERAL_TTL_SECONDS="3600") == 3600


def test_ephemeral_ttl_refuses_junk() -> None:
    """Like all integer knobs, junk should take the process down at import."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    probe = f"import sys; sys.path.insert(0, {SRC!r}); import config"
    run = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**clean, "CHAT_EPHEMERAL_TTL_SECONDS": "lots"},
    )
    assert run.returncode != 0
    assert "ValueError" in run.stderr
