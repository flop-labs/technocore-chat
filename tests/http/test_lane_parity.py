"""One logical write, performed three ways, asserting they land identically.

The three lanes to a room write: store.append directly against the root, the HTTP GET
say lane, and the MCP wrapper (whose say builds the same GET — see tests/test_mcp.py for
the urlopen-into-TestClient trick that makes the wrapper drive the real app here). If
the three ever disagree about what a record IS, the disagreement is invisible to every
single-lane test: this file is the differential check.

Seed of the §6.5 port gate: the assertions below are phrased against the protocol (one
JSONL record per write, field-identical modulo the fields named in each test; one
rendered line per message), never against this implementation's internals, so pointing
lanes (b) and (c) at a port of the service reuses this file as the gate unchanged.

Run: uv run --group dev python -m pytest tests
"""

from __future__ import annotations

import email.message
import io
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mcp" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

# Reused, not re-implemented: a second copy of the signing construction in a parity test
# would be a test that agrees with itself rather than with the service.
from _client import _keypair, _say_signed  # noqa: E402


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
    with config.override(ROOT=tmp_path):
        from technocore_mcp import server as mcp_server

        client = TestClient(app_module.app)

        class _Body:
            def __init__(self, text: str):
                self._text = text

            def read(self) -> bytes:
                return self._text.encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        def fake_urlopen(request, timeout=None):
            assert request.full_url.startswith(mcp_server.BASE_URL)
            response = client.get(request.full_url[len(mcp_server.BASE_URL) :])
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    request.full_url,
                    response.status_code,
                    "error",
                    email.message.Message(),
                    io.BytesIO(response.text.encode()),
                )
            return _Body(response.text)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(mcp_server, "DEFAULT_NICK", "")
        yield tmp_path, client, mcp_server


def call(server, name: str, arguments: dict) -> dict:
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert reply is not None and not reply["result"].get("isError"), reply
    return reply


def text_of(reply: dict) -> str:
    return reply["result"]["content"][0]["text"]


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
    # (c) the same write through the wrapper
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
    call(  # (c) the wrapper's write_note, which builds the same GET with safe="" quoting
        mcp_server.server, "write_note", {"namespace": "zz-parity", "key": "mcp", "value": value}
    )

    files = {
        key: store.note_path(root, "zz-parity", key).read_text(encoding="utf-8")
        for key in ("direct", "http", "mcp")
    }
    assert set(files.values()) == {value}

    # The read lane carries no per-write timestamp, so same key read both ways must be
    # byte-identical — the strictest form of the rendered parity above.
    http_read = client.get("/kv/zz-parity/http").text
    wrapped_read = text_of(
        call(mcp_server.server, "read_note", {"namespace": "zz-parity", "key": "http"})
    )
    assert wrapped_read == http_read


# --------------------------------------------------------------------------- name grammar
#
# One rejected name, asked through every write lane, asserting they all reject it the same
# way. The class of bug: a room class is a name PREFIX, so `room_classes` reads a class off
# a string the grammar refuses — and both write gates asked their class question before
# anything read the grammar. `mb-FOO` was answered 403 "send a signature", and the signed
# lane it named as the correction answered 400 "bad name". One write, two answers, and the
# 403 pointed at the lane that contradicted it.
#
# This lives here rather than beside the mailbox tests because that is exactly what this
# file is for: a disagreement between lanes is invisible to every single-lane test.
#
# The signed lane is REALLY signed. `_signer` runs before the gate, so an unsigned probe
# never reaches the name check at all — it is answered on the did:key, and a test that used
# a placeholder DID would assert nothing about names while appearing to cover the lane.

BAD_NAMES = ["mb-FOO", "d-FOO", "e-FOO", "p-FOO", "mb-p-FOO"]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_bad_room_name_is_a_400_on_every_write_lane(lanes, bad):
    _root, client, _mcp = lanes
    did, sign = _keypair()
    answers = {
        "unsigned say": client.get(f"/r/{bad}/say/bot/hi"),
        "signed say": _say_signed(client, bad, did, sign, "hi"),
        "post": client.post(f"/r/{bad}", json={"from": "bot", "text": "hi"}),
        "read": client.get(f"/r/{bad}"),
    }
    for lane, r in answers.items():
        assert r.status_code == 400, f"{lane} answered {r.status_code} for {bad!r}: {r.text[:140]}"
        assert "bad name" in r.text, f"{lane} did not name the grammar: {r.text[:140]}"


@pytest.mark.parametrize("bad", ["D-FOO", "d-FOO"])
def test_bad_owner_key_is_a_400_on_both_ownership_lanes(lanes, bad):
    # `room-owners` asked `store.ownable` first and `room-allow` validated on the way to the
    # note, so the identical key got 403 from one and 400 from the other.
    _root, client, _mcp = lanes
    did, _sign = _keypair()
    owners = client.get(f"/kv/room-owners/{bad}/set/{did}")
    allow = client.get(f"/kv/room-allow/{bad}/set/{did}")
    assert owners.status_code == 400 and "bad name" in owners.text, owners.text[:160]
    assert allow.status_code == 400 and "bad name" in allow.text, allow.text[:160]


def test_a_valid_class_name_still_gets_its_class_answer(lanes):
    # The mirror, and the reason this is an ordering fix rather than a new rejection: a name
    # the grammar ACCEPTS must still be answered on class grounds. Without this the tests
    # above pass just as happily if the gate started refusing every mailbox.
    _root, client, _mcp = lanes
    r = client.get("/r/mb-real/say/bot/hi")
    assert r.status_code == 403 and "mailbox" in r.text, r.text[:160]
