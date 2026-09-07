"""Run: uv run --group dev python -m pytest tests

0.9.0 moved the store's per-record JSON to orjson. Every room file already on disk was
written by the stdlib encoder, and rooms are append-only — so a single file will hold lines
from both encoders forever. That is only safe if the two produce the *same bytes* for the
shapes this store writes, which is what these pin. A difference would not fail loudly; it
would quietly change the format of the second half of every existing room.
"""

from __future__ import annotations

import json

import _client
import pytest


# The exact call the store used before the swap. Kept here rather than described, because
# "byte-identical to the old encoder" is meaningless without the old encoder to compare to.
def stdlib_line(rec: dict) -> bytes:
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


client = _client.client  # the shared TestClient fixture

RECORDS = [
    {"seq": 1, "ts": "2026-08-25T12:00:00.000000Z", "from": "alice", "text": "hello"},
    # A signed record: the DID and the int64 nonce are the widest values a record carries.
    {
        "seq": 2,
        "ts": "2026-08-25T12:00:00.000000Z",
        "from": "did:key:z6MkjchhfUsD6mmvni8mCdXHw216Xrm9bQe2mBEyuTPUp7cD",
        "text": "signed",
        "nonce": 9223372036854775807,
    },
    # ensure_ascii=False was load-bearing: these must stay raw UTF-8, not \\u escapes.
    {"seq": 3, "ts": "t", "from": "bot", "text": 'naïve café 日本語 🙂 — "quotes" back\\slash'},
    {"seq": 4, "ts": "t", "from": "bot", "text": "\t tab, newline-free, and / a slash"},
]


@pytest.mark.parametrize(
    "text",
    ["hello", 'naïve café 日本語 🙂 — "quotes" back\\slash', "\t tab and / a slash"],
    ids=["plain", "unicode", "escapes"],
)
def test_the_line_the_store_writes_is_byte_identical_to_the_old_encoder(tmp_path, text) -> None:
    """Through the real append, not by calling the encoder directly — the thing that must
    not change is the bytes that land in the file, and that includes the *key order* the
    record is built in. Comparing encoders in isolation passes happily while the store
    writes something else (an earlier version of this test did exactly that, and missed a
    deliberate OPT_SORT_KEYS regression)."""
    import store

    store.append(tmp_path, "room", "alice", text)
    raw = store.room_path(tmp_path, "room").read_bytes()
    rec = store._parse(raw.rstrip(b"\n"))
    assert rec is not None, "the store wrote a line its own parser cannot read"
    # The order `_write_record` builds the record in. Sorting or reordering keys is a
    # format change even though every value survives it.
    assert list(rec) == ["seq", "ts", "from", "text"]
    assert raw == stdlib_line(rec)


@pytest.mark.parametrize("rec", RECORDS, ids=["plain", "signed", "unicode", "escapes"])
def test_records_written_by_the_old_encoder_still_parse(rec) -> None:
    """Backwards compatibility with every room file already on disk. `_parse` is what reads
    them, and it is the one function a bad swap would break silently — it returns None for
    unparseable lines, so a regression would read as "the room is empty"."""
    import store

    assert store._parse(stdlib_line(rec).rstrip(b"\n")) == rec


def test_a_room_written_by_both_encoders_reads_back_whole(tmp_path) -> None:
    """The mixed-file case, end to end: old lines appended by hand, new lines appended by
    the store, one `read_messages` that must return all of them in order."""
    import store

    room = tmp_path / "rooms" / "mixed.jsonl"
    room.parent.mkdir(parents=True)
    with room.open("wb") as f:
        for seq in (1, 2):
            f.write(stdlib_line({"seq": seq, "ts": "t", "from": "old", "text": f"legacy {seq}"}))
    store.append(tmp_path, "mixed", "new", "written by orjson")

    view = store.read_messages(tmp_path, "mixed", limit=50)
    assert [m["text"] for m in view["messages"]] == ["legacy 1", "legacy 2", "written by orjson"]
    assert [m["seq"] for m in view["messages"]] == [1, 2, 3]


def test_the_body_parser_refuses_the_non_finite_literals_stdlib_allowed(client) -> None:
    """A deliberate tightening, pinned so it is a decision rather than a side effect. stdlib
    accepts bare `NaN` and `Infinity` — neither is JSON per RFC 8259 — and would have put a
    float nan into a record. The service already refuses to boot on a non-finite
    CHAT_MAX_WAIT for the same reason: it emits documents no strict parser will read.
    """
    for body in ('{"text": NaN}', '{"text": Infinity}', '{"text": -Infinity}'):
        got = client.post("/r/lobby", content=body, headers={"content-type": "application/json"})
        assert got.status_code == 400, f"{body} was not refused: {got.status_code}"
        assert "must be JSON" in got.text
    # …and ordinary JSON still works, or the above would pass against a parser that
    # rejects everything.
    assert client.post("/r/lobby", json={"from": "bot", "text": "fine"}).status_code in (200, 201)
