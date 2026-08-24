"""The unknown-argument rule, checked on both sides of the wrapper's own promise.

`tools/call` refuses an argument the tool does not declare. That refusal is only
correct if the schema `tools/list` publishes *says* so: JSON Schema permits properties
an object schema does not name unless `additionalProperties` is false, so a client that
validates locally against the advertised document reaches a different verdict than the
server does. That is the disagreement generating schemas from signatures exists to end.

No app and no network here — every call in this file is refused before the handler runs,
and urlopen is replaced with a raising stub to prove it.

Run: uv run python -m pytest tests/test_mcp_unknown_arguments.py -q
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp" / "src"))

# One value per JSON Schema type, so a call can be built from the advertised document
# rather than from a hand-kept copy of each signature.
_SAMPLE: dict[str, Any] = {"string": "x", "integer": 1, "number": 1.0, "boolean": True}


def _minimal(schema: dict[str, Any]) -> dict[str, Any]:
    """The smallest argument object the advertised schema calls valid."""
    properties = schema["properties"]
    arguments = {}
    for name in schema.get("required", ()):
        declared = properties[name]
        arguments[name] = declared["enum"][0] if "enum" in declared else _SAMPLE[declared["type"]]
    return arguments


def sent(server, message: dict) -> dict:
    """One reply, typed as the dict it is — the same seam tests/test_mcp.py's `call` uses,
    so `handle`'s Success | Failure | None union is narrowed in one place."""
    reply = server.handle(message)
    assert reply is not None, message
    return reply


def test_the_published_schema_states_the_unknown_argument_rule_tools_call_enforces(
    monkeypatch,
):
    """Every tool, both directions: the server refuses an undeclared argument, and its
    published schema has to close the object so a client validating against that schema
    refuses the same call rather than sending it and being told -32602."""
    from technocore_mcp import protocol
    from technocore_mcp import server as mcp_server

    def never(request, timeout=None):
        raise AssertionError(f"the network was reached: {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", never)

    listed = sent(mcp_server.server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = listed["result"]["tools"]
    assert tools, "the server advertises no tools"

    for tool in tools:
        schema = tool["inputSchema"]
        arguments = _minimal(schema)
        assert "colour" not in schema["properties"]  # the argument no tool declares

        reply = sent(
            mcp_server.server,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool["name"], "arguments": {**arguments, "colour": "blue"}},
            },
        )
        # The server's verdict: a caller error, refused before the handler and the network.
        assert reply["error"]["code"] == protocol.INVALID_PARAMS, tool["name"]
        assert "colour" in reply["error"]["message"], tool["name"]

        # The document's verdict has to match it. Without this an object schema admits
        # every property it does not name, so the same arguments validate clean.
        assert schema.get("additionalProperties") is False, (
            f"{tool['name']}: tools/call refuses unknown arguments and the advertised "
            "schema does not say so"
        )
