import json
from pathlib import Path

import pytest

# No sys.path insert: [tool.pytest.ini_options] pythonpath carries exporter/src, the same
# way it carries src/ for `import app`. This directory tests exporter/ the way tests/edge
# tests edge/ — CI runs `pytest tests` with an explicit path, so a suite outside tests/
# is silently never collected while its files still count against coverage.
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stats() -> dict:
    """A real `/stats` body, captured from a live service rather than written by hand.

    See fixtures/README.md for the exact traffic behind it. The numbers matter: this
    deployment holds a room that is both a mailbox and unlisted, and another that is
    unlisted and nothing else, which is what makes the class counts genuinely fail to
    partition rather than failing to partition only in principle.
    """
    return json.loads((FIXTURES / "stats.json").read_text())
