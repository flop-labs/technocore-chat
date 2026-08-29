"""Tests for Technocore Custom Autonomous Agent."""

from scripts.custom_agent import run_agent


def test_run_agent():
    """Verify that the custom agent runs successfully."""
    assert run_agent() is True
