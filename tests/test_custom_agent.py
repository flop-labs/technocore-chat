"""Tests for Technocore Custom Autonomous Agent."""

import sys
from pathlib import Path

# Add repository root to sys.path to resolve imports cleanly
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.custom_agent import run_agent  # noqa: E402


def test_run_agent():
    """Verify that the custom agent runs successfully."""
    assert run_agent() is True
