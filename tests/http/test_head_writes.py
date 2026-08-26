"""A HEAD request must never perform a write.

Every fetch-only agent can write here, which is the design. But HEAD is the verb every
monitor, link checker, prefetcher and URL unfurler sends to *look* without touching, and
those callers assume a metadata request is side-effect-free. Before the WriteRoute fix,
Starlette inferred {GET, HEAD} on every function route, so HEAD dispatched onto the write
handlers and created rooms and notes.
"""

import _client

client = _client.client  # the shared TestClient fixture


def test_a_head_to_a_room_say_lane_writes_nothing(client):
    r = client.head("/r/headroom/say/monitor/probe")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET"
    # The write must not have landed. A room read answers 200 whether or not the room
    # exists, so the assertion is on the contents, not the status.
    view = client.get("/r/headroom?format=json").json()
    assert view["count"] == 0


def test_a_head_to_a_note_set_lane_writes_nothing(client):
    r = client.head("/kv/headns/headkey/set/headval")
    assert r.status_code == 405
    # Unlike rooms, a missing note is a 404 — which is itself proof nothing was stored.
    assert client.get("/kv/headns/headkey").status_code == 404


def test_a_head_to_a_read_path_stays_a_read_even_where_post_lives(client):
    """/r/<room> and /kv/<ns>/<key> carry both lanes. HEAD belongs to the read half;
    it must answer from the reader, never fall through to the writer."""
    assert client.head("/r/headroom3").status_code == 200
    assert client.get("/r/headroom3?format=json").json()["count"] == 0
    assert client.head("/kv/headns3/headkey").status_code == 404
    assert client.post("/r/headroom4", json={"from": "a", "text": "b"}).status_code == 200
    # The POST route never offered HEAD in the first place; the read route above is what
    # answered, so a HEAD there keeps normal read semantics.


def test_reads_still_answer_head(client):
    """The read surface keeps HEAD semantics — metadata only, no body."""
    client.get("/r/readdoc/say/seer/something")
    r = client.head("/r/readdoc")
    assert r.status_code == 200
