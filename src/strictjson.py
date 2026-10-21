"""orjson's wire behavior, with unambiguous object names on hostile input."""

from __future__ import annotations

import json
from typing import Any

import orjson

OPT_APPEND_NEWLINE = orjson.OPT_APPEND_NEWLINE
dumps = orjson.dumps


def _unique_names(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    names = dict(pairs)
    if len(names) != len(pairs):
        raise ValueError("duplicate JSON key")
    return names


def _check_names(value: bytes) -> None:
    json.loads(value, object_pairs_hook=_unique_names)


def loads(value: bytes) -> Any:
    payload = orjson.loads(value)
    if isinstance(payload, dict) and value.count(b":") > len(payload):
        _check_names(value)
    return payload
