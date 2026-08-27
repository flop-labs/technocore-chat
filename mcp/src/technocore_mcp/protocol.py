"""The MCP wire protocol, by hand, over stdio. No dependencies.

Why not the SDK: this package exists so an agent runtime that speaks MCP can reach a
service whose whole premise is that you need nothing to reach it. Shipping a wrapper that
drags in a framework and a validation library to forward a handful of URL shapes would contradict
the thing it wraps — and `uvx technocore-mcp` with an empty dependency set starts in the
time it takes to unpack one wheel.

What it implements: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
`ping`, and JSON-RPC framing over newline-delimited stdio. That is the whole surface a
tools-only server needs; resources, prompts, sampling and completion are not advertised in
the capabilities block, so a spec-compliant client never calls them.

Two things in here are derived rather than declared, because a second copy of either is a
copy that drifts. The inbound message has a shape — `TypedDict`s below say what it is, and
narrowing functions check the parts that JSON cannot promise. And a tool's `inputSchema` is
read off its handler's signature: the function *is* the schema, so what `tools/list`
advertises and what `tools/call` enforces cannot disagree with what the handler accepts.
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from collections.abc import Callable
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    TextIO,
    TypedDict,
    TypeGuard,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

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

# Keep every accepted integer serializable under Python's lowest permitted nonzero
# int-string conversion limit. This is transport policy, not mutable interpreter policy.
MAX_JSON_INTEGER_DIGITS = 640


def _parse_int(token: str) -> int:
    digits = token.removeprefix("-")
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits")
    return int(token)


# ------------------------------------------------------------------ the message on the wire

# JSON-RPC 2.0 allows a string or a number and explicitly retired the null id of 1.0. This
# server takes str and int: a float id is a number a client cannot round-trip through every
# JSON implementation intact, and matching a reply to the wrong request is worse than a
# rejected one.
RequestId = str | int


class Notification(TypedDict):
    """A message with no `id` key. It gets no reply — not even for an unknown method."""

    jsonrpc: NotRequired[str]
    method: str
    params: NotRequired[dict[str, Any]]


class Request(Notification):
    """A notification plus the one key that makes a reply mandatory."""

    id: RequestId


class InitializeParams(TypedDict):
    """Everything a client may send in `initialize`; every key is optional to us."""

    protocolVersion: NotRequired[str]
    capabilities: NotRequired[dict[str, Any]]
    clientInfo: NotRequired[dict[str, Any]]


class CallParams(TypedDict):
    """`tools/call`: which tool, and the arguments its schema will be held to."""

    name: str
    arguments: NotRequired[dict[str, Any] | None]


class ErrorObject(TypedDict):
    code: int
    message: str


class Success(TypedDict):
    jsonrpc: Literal["2.0"]
    id: RequestId | None
    result: dict[str, Any]


class Failure(TypedDict):
    # `id` is None when the request never had a usable one — a parse error, or an id this
    # server refused. The spec asks for null there rather than an invented id.
    jsonrpc: Literal["2.0"]
    id: RequestId | None
    error: ErrorObject


Reply = Success | Failure


def _is_request_id(value: Any) -> TypeGuard[RequestId]:
    """`True` is an `int` in Python and is not one in JSON.

    A client that sent `"id": true` and got back a reply keyed `1` would pair it with a
    different request, so bool is checked out before the int check lets it through.
    """
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _is_request(message: dict[str, Any]) -> TypeGuard[Request]:
    """The rest of the request shape, once the caller has cleared the `id`."""
    return isinstance(message.get("method"), str)


def _is_initialize_params(params: dict[str, Any]) -> TypeGuard[InitializeParams]:
    """Nothing is required, so only the one key we read has to be the right type."""
    return isinstance(params.get("protocolVersion", ""), str)


def _is_call_params(params: dict[str, Any]) -> TypeGuard[CallParams]:
    arguments = params.get("arguments")
    return isinstance(params.get("name"), str) and (
        arguments is None or isinstance(arguments, dict)
    )


# ------------------------------------------------------------------ schemas from signatures

_JSON_TYPES: dict[type, str] = {str: "string", bool: "boolean", int: "integer", float: "number"}

_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    # JSON has one number type, so an integer is a number and `seconds: 0` is valid. A bool
    # is not a number here for the same reason it is not an id.
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
}


def fragment(annotation: Any) -> dict[str, Any]:
    """One parameter's JSON Schema, from its annotation.

    `Annotated[int, "..."]` carries the description the model reads; `Literal[...]` becomes
    an `enum`; and the `None` arm of `X | None` is dropped, because "may be left out" is
    said by the parameter having a default and lands in `required`, not in `type`.

    Anything with no mapping raises at import, where a wrong schema is a broken build
    rather than a tool that advertises one contract and enforces another.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        inner, *notes = get_args(annotation)
        described = fragment(inner)
        for note in notes:
            if isinstance(note, str):
                described["description"] = note
        return described
    if origin is Union or origin is types.UnionType:
        arms = [arm for arm in get_args(annotation) if arm is not type(None)]
        if len(arms) != 1:
            raise TypeError(f"cannot describe {annotation!r} in JSON Schema")
        return fragment(arms[0])
    if origin is Literal:
        values = get_args(annotation)
        named = {_JSON_TYPES.get(type(value)) for value in values}
        if len(named) != 1 or None in named:
            raise TypeError(f"cannot describe {annotation!r} in JSON Schema")
        return {"type": named.pop(), "enum": list(values)}
    if isinstance(annotation, type) and annotation in _JSON_TYPES:
        return {"type": _JSON_TYPES[annotation]}
    raise TypeError(f"no JSON Schema mapping for {annotation!r}")


def schema_of(handler: Callable[..., str]) -> dict[str, Any]:
    """A tool's `inputSchema`, read off the function that implements it.

    Required is "has no default" — the rule Python already enforces at the call, so a schema
    saying anything else would advertise a call that cannot happen. `required` is omitted
    entirely when nothing is required, which is what a client expects to see for a tool
    whose arguments are all optional.
    """
    called = getattr(handler, "__name__", "handler")
    hints = get_type_hints(handler, include_extras=True)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in inspect.signature(handler).parameters.items():
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            raise TypeError(f"{called}: {name!r} cannot arrive as a JSON object key")
        if name not in hints:
            raise TypeError(f"{called}: {name!r} needs an annotation to describe")
        properties[name] = fragment(hints[name])
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _validate(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Arguments against the advertised schema, before the handler and before the network.

    Everything here is the caller's mistake, so it is `-32602` and not a tool result: a
    client that sent `since: "1"` has a bug the model cannot fix by reading a nicer error,
    and letting it through would put a string where the service expects a seq. The other
    direction — a fetch that fails, a name the service rejects — is data the model *can* act
    on, and stays an `isError` result further down.

    Returns the arguments the handler is called with, which is not always the dict that
    arrived: JSON Schema reads `integer` by value and not by spelling, so `1.0` satisfies
    the schema this server advertised and is narrowed to `1` here. Rejecting it would fail
    a client that validated locally against our own document — the exact disagreement
    generating these schemas was meant to end — and passing the float through would put
    `?since=1.0` on the wire.
    """
    properties: dict[str, dict[str, Any]] = schema["properties"]
    unexpected = set(arguments) - set(properties)
    if unexpected:
        raise _BadParamsError(f"unexpected arguments: {', '.join(sorted(unexpected))}")
    missing = set(schema.get("required", ())) - set(arguments)
    if missing:
        raise _BadParamsError(f"missing arguments: {', '.join(sorted(missing))}")
    checked: dict[str, Any] = {}
    for name, value in arguments.items():
        expected = properties[name]
        kind = expected["type"]
        if kind == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)  # `is_integer()` is False for nan and inf, which stay rejected
        if not _CHECKS[kind](value):
            article = "an" if kind.startswith("i") else "a"  # integer is the only vowel here
            raise _BadParamsError(f"argument {name!r} must be {article} {kind}")
        if "enum" in expected and value not in expected["enum"]:
            allowed = ", ".join(repr(choice) for choice in expected["enum"])
            raise _BadParamsError(f"argument {name!r} must be one of: {allowed}")
        checked[name] = value
    return checked


# ------------------------------------------------------------------ tools


class Tool:
    """One callable tool: name, description, and the handler that is also its schema.

    `handler` returns the text the model sees. Raising is fine — a raised exception
    becomes an `isError` tool result rather than a JSON-RPC error, which is what the spec
    asks for: a failed tool call is data the model can react to, not a protocol fault.
    """

    def __init__(self, name: str, description: str, handler: Callable[..., str]):
        self.name = name
        self.description = description
        self.handler = handler
        self.schema = schema_of(handler)

    def spec(self) -> dict[str, Any]:
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

    def tool(self, name: str, description: str) -> Callable:
        """Register a handler as a tool. Its parameters describe themselves."""

        def register(fn: Callable[..., str]) -> Callable[..., str]:
            self.tools[name] = Tool(name, description, fn)
            return fn

        return register

    # ------------------------------------------------------------------ dispatch

    def handle(self, message: dict[str, Any]) -> Reply | None:
        """One request in, one response out — or None for a notification.

        Notifications (no `id` *key*) must never be answered, including when they name a
        method this server does not have: a response to a notification is a protocol
        violation that some clients treat as fatal. `"id": null` is not a notification —
        it is a request whose id this server refuses, and it gets told so.
        """
        if "id" not in message:
            return None
        ident = message["id"]
        if not _is_request_id(ident):
            return _error(None, INVALID_REQUEST, "request id must be a string or integer")
        if not _is_request(message):
            return _error(ident, INVALID_REQUEST, "missing method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            # By-position params: legal JSON-RPC, but no method here takes them, and
            # guessing which argument a client meant is worse than saying so.
            return _error(ident, INVALID_PARAMS, "params must be an object")
        method = message["method"]
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

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion") if _is_initialize_params(params) else None
        version = asked if asked in SUPPORTED_VERSIONS else LATEST_VERSION
        return {
            "protocolVersion": version,
            # Only what is implemented. `listChanged: False` because the tool set is fixed
            # at import — a client that believes otherwise would subscribe to nothing.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": self.instructions,
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        if not _is_call_params(params):
            raise _BadParamsError("tools/call needs a string `name` and an object `arguments`")
        tool = self.tools.get(params["name"])
        if tool is None:
            raise _BadParamsError(f"unknown tool {params['name']!r}")
        arguments = _validate(params.get("arguments") or {}, tool.schema)
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
                message = json.loads(line, parse_int=_parse_int)
            except (json.JSONDecodeError, ValueError) as exc:
                _write(stdout, _error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
                continue
            # Batches were removed in 2025-06-18 but older clients may still send one.
            response = _response(self, message)
            if response is not None:
                _write(stdout, response)


class _BadParamsError(ValueError):
    pass


def _response(server: Server, message: Any) -> Reply | list[Reply] | None:
    """The one JSON value to write back, or None when nothing may be written.

    A batch is answered by a single array, never by one top-level object per member: a
    client that sent a batch is waiting for one array and will either reject the loose
    objects or match replies to the wrong requests. A batch of nothing but notifications
    is answered by nothing at all, for the same reason a lone notification is.
    """
    if isinstance(message, list):
        if not message:
            return _error(None, INVALID_REQUEST, "batch must not be empty")
        replies: list[Reply] = []
        for member in message:
            if not isinstance(member, dict):
                replies.append(_error(None, INVALID_REQUEST, "batch member must be an object"))
            elif reply := server.handle(member):
                replies.append(reply)
        return replies or None
    if isinstance(message, dict):
        return server.handle(message)
    return _error(None, INVALID_REQUEST, "message must be an object")


def _ok(ident: RequestId | None, result: dict[str, Any]) -> Success:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: RequestId | None, code: int, message: str) -> Failure:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, message: Reply | list[Reply]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdout.flush()
