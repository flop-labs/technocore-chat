"""GET /r/<room>/export — the retained ring as one raw JSONL download (design §5.1–§5.2).

Byte-exactness is the whole contract: a signed record must re-verify from the exported
line alone, so every test here compares against the stored file's own bytes and never
against a re-rendering of them.
"""

import json

import _client
from _client import _keypair, _say_signed

import didkey
import store

client = _client.client  # the shared TestClient fixture


def test_export_is_byte_identical_to_the_stored_file(client, tmp_path):
    for i in range(3):
        client.get(f"/r/dump/say/alice/message%20{i}")
    did, sign = _keypair()
    assert _say_signed(client, "dump", did, sign, "signed line", nonce=7).status_code == 200

    r = client.get("/r/dump/export")
    assert r.status_code == 200
    assert r.content == store.room_path(tmp_path, "dump").read_bytes()
    assert r.headers["content-type"] == "application/x-ndjson; charset=utf-8"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_a_signed_record_reverifies_from_the_exported_bytes_alone(client):
    """The offline-verification promise (#66/#93): an exporter holding nothing but the
    dump and the room name can check who said what. stdlib json, deliberately — its ints
    are exact at any width, which is what the manual tells re-verifiers to use."""
    did, sign = _keypair()
    assert (
        _say_signed(client, "proofs", did, sign, "attributable claim", nonce=3).status_code == 200
    )

    lines = client.get("/r/proofs/export").content.splitlines()
    signed = [rec for rec in map(json.loads, lines) if "sig" in rec]
    assert len(signed) == 1
    rec = signed[0]
    didkey.verify(rec["from"], rec["sig"], f"proofs|{rec['nonce']}|{rec['text']}")


def test_a_torn_final_line_is_excluded(client, tmp_path):
    client.get("/r/torn/say/bot/whole%20line")
    path = store.room_path(tmp_path, "torn")
    whole = path.read_bytes()
    with path.open("ab") as f:
        f.write(b'{"seq": 2, "ts": "2026-')  # a crash mid-append: no newline, no record
    assert client.get("/r/torn/export").content == whole


def test_a_room_that_is_only_a_torn_line_exports_nothing(client, tmp_path):
    client.get("/r/allgone/say/bot/x")
    path = store.room_path(tmp_path, "allgone")
    path.write_bytes(b'{"seq": 1, "ts"')
    assert client.get("/r/allgone/export").content == b""


def test_a_missing_room_answers_exactly_as_the_room_read_does(client, tmp_path):
    """Same by-name reachability, same absent-room behaviour: 200 and nothing, never a
    404 that would distinguish "empty" from "unread". And reading is not creating — the
    export must leave no file and never run the reaper."""
    read = client.get("/r/never-was")
    exported = client.get("/r/never-was/export")
    assert (read.status_code, exported.status_code) == (200, 200)
    assert exported.content == b""
    assert exported.headers["x-room-generation"] == "0"
    assert not store.room_path(tmp_path, "never-was").exists()
    assert not (tmp_path / ".reaped").exists()  # _reap marks every pass with this file

    # A malformed name refuses on the same terms as the read lane.
    assert client.get("/r/UPPER/export").status_code == client.get("/r/UPPER").status_code == 400


def test_the_generation_header_matches_the_read_view_and_moves_with_the_epoch(client, tmp_path):
    client.get("/r/epoch/say/bot/hello")
    view = client.get("/r/epoch?format=json").json()
    assert client.get("/r/epoch/export").headers["x-room-generation"] == str(view["generation"])

    # Reap and recreate: a dump of the new conversation must not stamp itself as the old.
    _client._age(store.room_path(tmp_path, "epoch"), store.IDLE_SECONDS + 60)
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    client.get("/r/epoch/say/bot/hello%20again")
    after = client.get("/r/epoch?format=json").json()["generation"]
    assert after == view["generation"] + 1
    assert client.get("/r/epoch/export").headers["x-room-generation"] == str(after)


def test_export_spends_the_read_budget_and_is_not_a_free_path(client):
    import app as app_module
    import config
    import limit

    assert "export" not in limit.FREE_PATHS
    with config.override(RATE_READ=1):
        app_module._buckets.clear()
        assert client.get("/r/lobby/export").status_code == 200
        refused = client.get("/r/lobby/export")
        assert refused.status_code == 429
        assert "the read budget" in refused.text


def test_export_is_documented_where_the_protocol_is(client):
    spec = client.get("/openapi.json").json()
    assert "/r/{room}/export" in spec["paths"]
    exported = spec["paths"]["/r/{room}/export"]["get"]
    assert "application/x-ndjson" in exported["responses"]["200"]["content"]
    assert "X-Room-Generation" in exported["responses"]["200"]["headers"]
    assert "GET /r/<room>/export" in client.get("/llms.txt").text
