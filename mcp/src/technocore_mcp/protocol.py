"""The MCP wire protocol, by hand, over stdio. No dependencies.

Why not the SDK: this package exists so an agent runtime that speaks MCP can reach a
service whose whole premise is that you need nothing to reach it. Shipping a wrapper that
drags in a framework and a validation library to forward eight URL shapes would contradict
the thing it wraps — and `uvx technocore-mcp` with an empty dependency set starts in the
time it takes to unpack one wheel.

What it implements: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
`ping`, and JSON-RPC framing over newline-delimited stdio. That is the whole surface a
tools-only server needs; resources, prompts, sampling and completion are not advertised in
the capabilities block, so a spec-compliant client never calls them.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

# Versions this server understands. A client asks for one in `initialize`; the spec says
# reply with the same version if it is supported, otherwise with one this server does
# support and let the client decide whether to continue.
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_VERSION = SUPPORTED_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Tool:
    """One callable tool: name, description, JSON Schema, handler.

    `handler` returns the text the model sees. Raising is fine — a raised exception
    becomes an `isError` tool result rather than a JSON-RPC error, which is what the spec
    asks for: a failed tool call is data the model can react to, not a protocol fault.
    """

    def __init__(self, name: str, description: str, schema: dict, handler: Callable[..., str]):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


class Server:
    def __init__(self, name: str, version: str, instructions: str = ""):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools: dict[str, Tool] = {}

    def tool(self, name: str, description: str, schema: dict) -> Callable:
        def register(fn: Callable[..., str]) -> Callable[..., str]:
            self.tools[name] = Tool(name, description, schema, fn)
            return fn

        return register

    # ------------------------------------------------------------------ dispatch

    def handle(self, message: dict) -> dict | None:
        """One request in, one response out — or None for a notification.

        Notifications (no `id`) must never be answered, including when they name a method
        this server does not have: a response to a notification is a protocol violation
        that some clients treat as fatal.
        """
        if "id" not in message:
            return None
        ident = message["id"]
        if isinstance(ident, bool) or not isinstance(ident, (str, int)):
            return _error(None, INVALID_REQUEST, "request id must be a string or integer")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(method, str):
            return _error(ident, INVALID_REQUEST, "missing method")
        try:
            if method == "initialize":
                return _ok(ident, self._initialize(params))
            if method == "ping":
                return _ok(ident, {})
            if method == "tools/list":
                return _ok(ident, {"tools": [t.spec() for t in self.tools.values()]})
            if method == "tools/call":
                return _ok(ident, self._call(params))
        except _BadParamsError as exc:
            return _error(ident, INVALID_PARAMS, str(exc))
        except Exception as exc:  # a bug in this server, not in the caller
            return _error(ident, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return _error(ident, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _initialize(self, params: dict) -> dict:
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else LATEST_VERSION
        return {
            "protocolVersion": version,
            # Only what is implemented. `listChanged: False` because the tool set is fixed
            # at import — a client that believes otherwise would subscribe to nothing.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": self.instructions,
        }

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _BadParamsError(f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _BadParamsError("arguments must be an object")
        unexpected = set(arguments) - set(tool.schema.get("properties", {}))
        if unexpected:
            raise _BadParamsError(f"unexpected arguments: {', '.join(sorted(unexpected))}")
        missing = set(tool.schema.get("required", [])) - set(arguments)
        if missing:
            raise _BadParamsError(f"missing arguments: {', '.join(sorted(missing))}")
        try:
            body = tool.handler(**arguments)
        except Exception as exc:
            # A failed fetch, a 429, a rejected name: the model can act on all of these,
            # so they are results with isError, not JSON-RPC errors.
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": body}], "isError": False}

    # ------------------------------------------------------------------ transport

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """Newline-delimited JSON-RPC on stdio, the transport every MCP client supports.

        stdout carries protocol and nothing else — anything this process wants to say to a
        human goes to stderr, because one stray print corrupts the stream.
        """
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(stdout, _error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
                continue
            # Batches were removed in 2025-06-18 but older clients may still send one.
            response = _response(self, message)
            if response is not None:
                _write(stdout, response)


class _BadParamsError(ValueError):
    pass


def _response(server: Server, message: Any) -> dict | list[dict] | None:
    """The one JSON value to write back, or None when nothing may be written.

    A batch is answered by a single array, never by one top-level object per member: a
    client that sent a batch is waiting for one array and will either reject the loose
    objects or match replies to the wrong requests. A batch of nothing but notifications
    is answered by nothing at all, for the same reason a lone notification is.
    """
    if isinstance(message, list):
        if not message:
            return _error(None, INVALID_REQUEST, "batch must not be empty")
        replies: list[dict] = []
        for member in message:
            if not isinstance(member, dict):
                replies.append(_error(None, INVALID_REQUEST, "batch member must be an object"))
            elif reply := server.handle(member):
                replies.append(reply)
        return replies or None
    if isinstance(message, dict):
        return server.handle(message)
    return _error(None, INVALID_REQUEST, "message must be an object")


def _ok(ident: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, message: dict | list[dict]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdout.flush()
