import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp" / "src"))


@pytest.mark.parametrize("params", [[], "", 0, False, None])
def test_falsey_non_object_params_are_rejected(params):
    """Present falsey params must not be collapsed into the missing-params default."""
    from technocore_mcp import protocol

    server = protocol.Server("test", "0")
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params})
    assert reply == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": protocol.INVALID_PARAMS,
            "message": "params must be an object",
        },
    }
