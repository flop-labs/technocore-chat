import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp" / "src"))


def test_room_annotation_advertises_its_pattern():
    """tools/list must publish the Room name regex as a JSON-Schema `pattern`.

    `Room` is declared as `Annotated[str, "Room name", re.compile("^[a-z0-9]...$")]`, and the
    module's own design rule is that the advertised schema and what `tools/call`
    enforces cannot disagree. Today the regex never reaches the schema (only the
    description string), so a client that validates locally against it reaches
    `valid` and is then told `-32602` over the wire (#488)."""
    from technocore_mcp import protocol
    from technocore_mcp import server as mcp_server

    schema = protocol.fragment(mcp_server.Room)
    assert schema.get("pattern") == r"^[a-z0-9][a-z0-9_-]{0,47}$"


def test_malformed_room_name_is_refused_before_the_network():
    """A Room-typed argument carrying a name the service rejects must raise
    `-32602` at `_validate` time, not surface as an `isError` tool result after
    a network round-trip to a 400 (#488)."""
    from technocore_mcp import protocol
    from technocore_mcp import server as mcp_server

    def handler(room: mcp_server.Room) -> str:
        return room

    schema = protocol.schema_of(handler)
    assert "pattern" in schema["properties"]["room"]

    # A name the service (NAME_RE.fullmatch) refuses must be caught here.
    with pytest.raises(protocol._BadParamsError):
        protocol._validate({"room": "Bad Name!"}, schema)
    # A well-formed name still passes.
    assert protocol._validate({"room": "lobby"}, schema) == {"room": "lobby"}


def test_other_tools_keep_advertising_only_a_description():
    """Regression guard: a plain prose note on a `str` parameter is still carried
    as `description` and never mistaken for a pattern (#488)."""
    import typing

    from technocore_mcp import protocol

    note = protocol.fragment(typing.Annotated[str, "Message body, <= 4096 characters."])
    assert note.get("description") == "Message body, <= 4096 characters."
    assert "pattern" not in note
