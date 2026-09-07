"""One logical write, performed three ways, asserting they land identically.

The three lanes to a room write: store.append directly against the root, the HTTP GET
say lane, and the MCP wrapper (whose say goes over the service's POST lane — its
`use_fetch` seam is what points it at the TestClient, so it drives the real app here).
If the three ever disagree about what a record IS, the disagreement is invisible to
every single-lane test: this file is the differential check — and with the wrapper on
POST it now spans both write lanes of the HTTP surface, not two spellings of one.

Seed of the §6.5 port gate: the assertions below are phrased against the protocol (one
JSONL record per write, field-identical modulo the fields named in each test; one
rendered line per message), never against this implementation's internals, so pointing
lanes (b) and (c) at a port of the service reuses this file as the gate unchanged.

Run: uv run --group dev python -m pytest tests
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mcp" / "src"))


@pytest.fixture()
def lanes(tmp_path, monkeypatch):
    """One root, one app, three writers aimed at it: the store, HTTP, the wrapper.

    Modeled on the `mcp` fixture in tests/test_mcp.py, with the TestClient yielded
    alongside the wrapper so the HTTP lane and the wrapper write the SAME tree — parity
    only means anything when all three lanes share a root.
    """
    import app as app_module
    import config

    app_module._buckets.clear()
    # The filter is pinned off because this file's whole premise is three lanes writing
    # the SAME nick and text into one room — legal chat that a default-on cross-sender
    # filter could refuse at a threshold two copies lower than whatever ships. Pinning it
    # keeps the parity assertion about the lanes, not about today's DUPE_MAX_COPIES.
    with config.override(ROOT=tmp_path, DUPE_FILTER_SECONDS=0):
        from technocore_mcp import server as mcp_server

        client = TestClient(app_module.app)

        async def fetch(method, url, headers, body, timeout):
            assert url.startswith(mcp_server.BASE_URL)
            # TestClient drives the app on its own portal thread, so the blocking call is
            # not blocking the loop this coroutine runs on.
            response = client.request(
                method, url[len(mcp_server.BASE_URL) :], content=body, headers=headers
            )
            return response.status_code, response.text

        monkeypatch.setattr(mcp_server, "_fetch", fetch)
        monkeypatch.setattr(mcp_server, "DEFAULT_NICK", "")
        yield tmp_path, client, mcp_server


def call(server, name: str, arguments: dict) -> CallToolResult:
    reply = anyio.run(server.call_tool, name, arguments)
    assert not reply.is_error, reply
    return reply


def text_of(reply: CallToolResult) -> str:
    return "".join(block.text for block in reply.content if block.type == "text")


def test_one_room_write_lands_identically_through_all_three_lanes(lanes):
    """Three appends to one room file: the records must be the same shape and content.

    Legitimately differing fields, and why: `seq` (assigned under the room lock in
    arrival order, so the three sequential writes are 1, 2, 3 — that ordering IS the
    parity being checked, not a divergence) and `ts` (microsecond wall clock at write
    time). `nick` does NOT differ: the wrapper's nick argument becomes the same path
    segment the bare GET carries, so all three lanes write `from`: bot. Unsigned
    throughout — the signed lanes add `did`/`nonce` and are their own parity check.
    """
    import json

    import store

    root, client, mcp_server = lanes
    text = "hello parity"
    store.append(root, "parity", "bot", text)  # (a) the store, directly
    # (b) the HTTP GET lane — %20 proves the encoding path both lanes must share
    assert client.get("/r/parity/say/bot/hello%20parity").status_code == 200
    # (c) the same write through the wrapper, which takes the POST lane
    call(mcp_server.server, "say", {"room": "parity", "text": text, "nick": "bot"})

    records = [
        json.loads(line)
        for line in store.room_path(root, "parity").read_text(encoding="utf-8").splitlines()
    ]
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert len({tuple(sorted(r)) for r in records}) == 1  # one field set, from every lane
    assert {r["from"] for r in records} == {"bot"}
    assert {r["text"] for r in records} == {text}


def test_the_rendered_line_an_agent_sees_is_the_same_over_http_and_the_wrapper(lanes):
    """The write reply of each lane, rendered: identical except where they must differ.

    Two fresh rooms, because a write's reply is the room tail — a shared room would put
    different seq and content in each reply. The two replies then differ only in the
    room name (header and footer lines) and the `[seq] ts` prefix of the message line;
    everything else — banner, counts, attribution, text — must be byte-identical.
    """
    root, client, mcp_server = lanes
    http = client.get("/r/zz-http/say/bot/same%20words").text
    wrapped = text_of(
        call(mcp_server.server, "say", {"room": "zz-mcp", "text": "same words", "nick": "bot"})
    )

    def normalized(body: str, room: str) -> list[str]:
        return [
            re.sub(r"^\[\d+\] \S+ ", "[seq ts] ", line.replace(room, "<room>"))
            for line in body.splitlines()
        ]

    assert normalized(http, "zz-http") == normalized(wrapped, "zz-mcp")
    # …and the message line both agents see is the attribution and the text, nothing else.
    assert [line for line in normalized(http, "zz-http") if line.startswith("[seq ts]")][0] == (
        "[seq ts] <~bot> same words"
    )


def test_one_note_write_lands_identically_through_all_three_lanes(lanes):
    """Notes fall out the same way: one value, three lanes, byte-identical files, and a
    read surface with no timestamps at all — so the HTTP body and the wrapper's reply
    text are byte-for-byte equal with nothing masked."""
    import store

    root, client, mcp_server = lanes
    value = "parity value"
    store.note_set(root, "zz-parity", "direct", value)  # (a) the store, directly
    assert client.get("/kv/zz-parity/http/set/parity%20value").status_code == 200  # (b)
    call(  # (c) the wrapper's write_note, over the service's POST lane
        mcp_server.server, "write_note", {"namespace": "zz-parity", "key": "mcp", "value": value}
    )

    files = {
        key: store.note_path(root, "zz-parity", key).read_text(encoding="utf-8")
        for key in ("direct", "http", "mcp")
    }
    assert set(files.values()) == {value}

    # HTTP keeps the untrusted banner; MCP read_note strips framing so if_matches CAS
    # works (same contract as /humans noteValue). Stored file bytes are the parity that
    # must stay identical across lanes.
    http_read = client.get("/kv/zz-parity/http").text
    wrapped_read = text_of(
        call(mcp_server.server, "read_note", {"namespace": "zz-parity", "key": "http"})
    )
    assert "UNTRUSTED CONTENT" in http_read
    assert wrapped_read == value
    assert value in http_read
