"""orjson's wire behavior, with unambiguous object names on hostile input."""

from __future__ import annotations

import json
from typing import Any

import orjson

OPT_APPEND_NEWLINE = orjson.OPT_APPEND_NEWLINE
dumps = orjson.dumps


def _unique_names(pairs: list[tuple[str, object]]) -> None:
    if len(dict(pairs)) != len(pairs):
        raise ValueError("duplicate JSON key")


def loads(value: bytes) -> Any:
    payload = orjson.loads(value)
    if isinstance(payload, dict) and value.count(b":") > len(payload):
        json.loads(value, object_pairs_hook=_unique_names)
    return payload
