"""JSON-RPC envelope validation for the stdio MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp" / "src"))

from technocore_mcp import protocol  # noqa: E402


@pytest.mark.parametrize("version", [None, "1.0", 2, True])
def test_mcp_requests_require_jsonrpc_2(version):
    server = protocol.Server("test", "0")
    message = {"id": 7, "method": "ping"}
    if version is not None:
        message["jsonrpc"] = version

    reply = server.handle(message)

    assert reply == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": protocol.INVALID_REQUEST,
            "message": 'jsonrpc must be "2.0"',
        },
    }


def test_mcp_jsonrpc_2_request_still_succeeds():
    server = protocol.Server("test", "0")
    assert server.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {},
    }


def test_valid_notification_remains_silent():
    server = protocol.Server("test", "0")
    assert server.handle({"jsonrpc": "2.0", "method": "ping"}) is None
