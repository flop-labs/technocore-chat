import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp" / "src"))

from technocore_mcp import protocol  # noqa: E402


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "method": 1},
    ],
)
def test_malformed_no_id_objects_are_invalid_requests(message):
    server = protocol.Server("test", "test")

    assert server.handle(message) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": protocol.INVALID_REQUEST, "message": "missing method"},
    }


def test_malformed_no_id_batch_member_is_not_silently_dropped():
    server = protocol.Server("test", "test")

    assert protocol._response(
        server,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": 1},
            {"jsonrpc": "2.0", "method": "ping"},
        ],
    ) == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": protocol.INVALID_REQUEST, "message": "missing method"},
        },
    ]
