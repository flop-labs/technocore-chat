"""The room-view JSON response shape, held identical across every operation that
publishes it, and closed against silent additions.

Five operations share `_ROOM_VIEW_SCHEMA` (manifest.py): `readRoom`, `discoverRooms`,
`say`, `saySigned`, `postMessage`. The schema is generated from the enforced constants,
which is the property that makes it safe to consume — but only while every field the
server emits is named there. `generation` (aa7017f) and `posted` (present on every write
200 since forever) both sat outside the schema, so a strict schema-driven client parsed
them off. This file pins that: the shape the server returns is the shape the schema
promises, or the suite refuses.

Run: uv run --group dev python -m pytest tests
"""

from __future__ import annotations

import _client
from _client import _keypair

client = _client.client


# The five operations that share `_ROOM_VIEW_SCHEMA`. Kept as (path, verb) so a schema
# reference added later — a new write lane, an events-lane variant — surfaces here as one
# more tuple rather than in a scattered set of tests that all quietly agree.
_SHARED_OPERATIONS = [
    ("/r/{room}", "get"),
    ("/r/{room}", "post"),
    ("/r/{room}/say/{nick}/{text}", "get"),
    ("/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}", "get"),
    ("/r/events", "get"),
]


def _schema_for(document: dict, path: str, verb: str) -> dict:
    """The 200 JSON response schema for one operation, straight from the served document."""
    return document["paths"][path][verb]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]


def _validate(payload: dict, schema: dict) -> None:
    """The narrow validator this file needs: required fields present, no undeclared keys
    when `additionalProperties: false`. Hand-rolled because the suite pulls in no schema
    library outside the contract group, and one that would validate at import time here
    would then need pinning and vendoring in a place unrelated to the test itself."""
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in payload]
    assert not missing, f"required fields missing from response: {missing}"
    if schema.get("additionalProperties") is False:
        extra = [key for key in payload if key not in properties]
        assert not extra, (
            f"response carries fields the schema does not declare: {extra} — "
            "add them to _ROOM_VIEW_SCHEMA or stop emitting them"
        )


def test_room_view_schema_is_shared_across_all_five_operations(client):
    """Every one of the five operations must reference the exact same object. That
    identity is what makes it correct to write one validator here and reuse it — and the
    property drift can hide behind, since two schemas that started identical can diverge
    by the same silent edit."""
    document = client.get("/openapi.json").json()
    first = _schema_for(document, *_SHARED_OPERATIONS[0])
    for path, verb in _SHARED_OPERATIONS[1:]:
        assert _schema_for(document, path, verb) == first, (
            f"{verb.upper()} {path}'s 200 JSON schema disagrees with "
            f"{_SHARED_OPERATIONS[0][1].upper()} {_SHARED_OPERATIONS[0][0]}"
        )


def test_room_view_schema_declares_the_fields_the_server_actually_returns(client):
    """`generation` (aa7017f) and `posted` (every write 200) must both be in properties.
    Kept as a name check — not a shape check — so a rename in the store or the handler
    lands here and cannot be papered over by adjusting the runtime alone."""
    schema = _schema_for(client.get("/openapi.json").json(), *_SHARED_OPERATIONS[0])
    for field in ("generation", "posted"):
        assert field in schema["properties"], f"{field} missing from _ROOM_VIEW_SCHEMA.properties"


def test_room_view_schema_is_closed_against_further_drift(client):
    """`additionalProperties: false` is what makes the contract check catch the next
    unnamed field before it ships. Without it, a schema that lists five things is a
    contract about five things and a permission to return any others."""
    schema = _schema_for(client.get("/openapi.json").json(), *_SHARED_OPERATIONS[0])
    assert schema.get("additionalProperties") is False


def test_readroom_json_matches_the_schema(client):
    """A read is `_ROOM_VIEW_SCHEMA` without `posted`: the write-only field is optional
    in the schema and absent here, and `generation` is present because it is required."""
    client.get("/r/lobby/say/alice/hello%20schema")
    payload = client.get("/r/lobby?format=json").json()
    schema = _schema_for(client.get("/openapi.json").json(), "/r/{room}", "get")
    _validate(payload, schema)
    assert "generation" in payload and isinstance(payload["generation"], int)
    # A read must never smuggle a write-only field back — separate assertion so the
    # failure names which side of the contract split it broke.
    assert "posted" not in payload


def test_say_write_lane_includes_posted_and_matches_the_schema(client):
    """The GET write lane returns view + `posted`. Both must be declared, and no third
    field may appear."""
    payload = client.get("/r/lobby/say/bob/hello%20from%20say?format=json").json()
    schema = _schema_for(client.get("/openapi.json").json(), "/r/{room}/say/{nick}/{text}", "get")
    _validate(payload, schema)
    assert payload["posted"]["text"] == "hello from say"
    assert payload["posted"]["seq"] == payload["last_seq"]


def test_post_lane_matches_the_schema(client):
    """The POST lane shares the schema with the GET lanes, and the extra fields it may
    end up with (a signed post carries `did`/`sig`/`nonce` in the *request*, not the
    response) must not leak into the reply."""
    payload = client.post(
        "/r/lobby", json={"from": "carol", "text": "hello from post"}, params={"format": "json"}
    ).json()
    schema = _schema_for(client.get("/openapi.json").json(), "/r/{room}", "post")
    _validate(payload, schema)
    assert payload["posted"]["text"] == "hello from post"


def test_signed_write_lane_matches_the_schema(client):
    """The signed lane returns the same view, so a schema-driven client that reads a
    signed message off `posted` cannot silently break because it took the signed path.
    `?format=json` on the signed lane is the same query param every other lane honours."""
    import store

    did, sign = _keypair()
    body = store.clean_text("hello signed")
    signature = sign(f"lobby|1|{body}")
    reply = client.get(f"/r/lobby/say-signed/{did}/{signature}/1/hello%20signed?format=json")
    assert reply.status_code == 200
    payload = reply.json()
    schema = _schema_for(
        client.get("/openapi.json").json(),
        "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}",
        "get",
    )
    _validate(payload, schema)
    assert payload["posted"]["from"] == did and payload["posted"]["nonce"] == 1


def test_events_room_read_matches_the_schema(client):
    """`/r/events` shares the schema with every other room; a client that treats it as a
    special-case string of lines has already lost the machine-readable field the schema
    exists to give it."""
    # Create a public room so events has something to log — otherwise the reader would
    # trivially satisfy the schema with an empty messages array.
    client.get("/r/rendezvous/say/dave/hi")
    payload = client.get("/r/events?format=json").json()
    schema = _schema_for(client.get("/openapi.json").json(), "/r/events", "get")
    _validate(payload, schema)
    assert payload["room"] == "events"
