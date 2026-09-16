"""Responses are compressed on the wire, and decode to exactly what they said before.

The failure this file exists to catch is silent. `application/x-ndjson` is not in
starlette-compress's default allow-list, so without the `add_compress_type` call in app.py
the export — the largest lane on the wire by a wide margin — would keep going out as
plaintext while every other route compressed, and nothing else in the suite would notice:
the status is 200, the bytes are right, only the metered leg is wrong.
"""

import _client
from _client import _keypair, _say_signed

import store

client = _client.client  # the shared TestClient fixture


def _raw(client, path: str, encoding: str = "gzip"):
    """A read with the encoding a caller offered. httpx decodes the body for us and leaves
    `Content-Encoding` in place, so the header says what went over the wire while
    `.content` is what the caller ends up with — which is exactly the pair worth asserting."""
    return client.get(path, headers={"Accept-Encoding": encoding})


def test_export_is_compressed(client):
    """NDJSON is the added content type, and the one that pays for the whole change."""
    for i in range(400):
        client.get(f"/r/bulk/say/alice/message%20{i}%20with%20enough%20text%20to%20pass%20500B")

    r = _raw(client, "/r/bulk/export")
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()


def test_export_still_decodes_to_the_stored_bytes(client, tmp_path):
    """Byte-exactness is the export's whole contract (design §5.1–§5.2). Compression is a
    transport encoding, so it may not survive as an excuse for changing a single byte."""
    did, sign = _keypair()
    assert _say_signed(client, "proof", did, sign, "attributable claim", nonce=3).status_code == 200
    for i in range(400):
        client.get(f"/r/proof/say/alice/padding%20line%20{i}%20to%20clear%20the%20minimum")

    raw = _raw(client, "/r/proof/export")
    assert raw.headers["content-encoding"] == "gzip"
    assert raw.content == store.room_path(tmp_path, "proof").read_bytes()


def test_a_room_read_is_compressed(client):
    for i in range(200):
        client.get(f"/r/chatty/say/bob/line%20{i}%20padded%20out%20past%20the%20minimum%20size")

    r = _raw(client, "/r/chatty?limit=200")
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"


def test_a_caller_that_asks_for_nothing_gets_plaintext(client):
    """The stdlib-urllib lane this service targets sends no Accept-Encoding at all."""
    for i in range(200):
        client.get(f"/r/plain/say/bob/line%20{i}%20padded%20out%20past%20the%20minimum%20size")

    r = _raw(client, "/r/plain?limit=200", encoding="identity")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


def test_a_small_reply_is_left_alone(client):
    """Below minimum_size the framing would cost more than it saves."""
    r = _raw(client, "/healthz")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
