"""Regression checks for CI service readiness gates."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
HELPER = ROOT / "scripts/wait-for-health.sh"


def test_ci_workflows_use_a_failing_readiness_gate():
    """A failed health poll must stop CI before later probes hide the boot failure."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    helper = HELPER.read_text(encoding="utf-8")

    assert workflow.count("bash scripts/wait-for-health.sh") == 2
    assert "exit 1" in helper
    assert "::error::" in helper
