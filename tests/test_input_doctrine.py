"""The reports that produced the input doctrine, pinned one test per issue.

tests/test_contract.py beside this file is the generative half: it will find the *next*
parameter that drifts. These are the seven concrete requests that were reported before it
existed, kept as plain pytest so a regression names the issue it re-opens rather than
arriving as a shrunk hypothesis example. The rule they are all instances of is in
docs/design.md §3.5 — advisory parameters clamp, semantic ones refuse naming the field.
"""

from __future__ import annotations

import _client

client = _client.client  # the shared TestClient fixture


def test_372_an_out_of_range_rooms_limit_clamps_rather_than_refusing(client):
    """`/rooms?limit=-5` answered 200 against a published `minimum: 1`. Advisory shape, so
    the 200 is right and the published `minimum` was the wrong half — the schema now states
    the fallback in prose and publishes no bound the handler does not enforce."""
    assert client.get("/rooms?limit=-5").status_code == 200
    limit = next(
        p
        for p in client.get("/openapi.json").json()["paths"]["/rooms"]["get"]["parameters"]
        if p["name"] == "limit"
    )
    assert "minimum" not in limit["schema"] and "maximum" not in limit["schema"]
    assert "falls back to 50" in limit["description"]


def test_372_a_zero_rooms_limit_clamps_to_one_room(client):
    """`limit=0` survives `_cursor` (it is a non-negative int) and is floored to 1 by the
    `or 1`. Also advisory, also a 200 — and the description now says so, where the old
    `minimum: 1` implied a refusal that never happened."""
    client.get("/r/lobby/say/bot/hi")
    view = client.get("/rooms?limit=0&format=json").json()
    assert len(view["rooms"]) == 1 and view["total"] >= 1


def test_372_an_unrecognised_format_falls_back_to_text_rather_than_refusing(client):
    """`?format=garbage123` was 200 text/plain against a published `enum: ["json"]`. The
    fallback stays; the enum is gone, and the description names the fallback so a caller
    checks the Content-Type instead of trusting a constraint nobody enforced."""
    response = client.get("/rooms?format=garbage123")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    fmt = next(
        p
        for p in client.get("/openapi.json").json()["paths"]["/rooms"]["get"]["parameters"]
        if p["name"] == "format"
    )
    assert "enum" not in fmt["schema"] and "stays text/plain" in fmt["description"]


def test_427_a_non_string_from_is_refused_rather_than_str_coerced(client):
    """`{"from": 0}` was stored as the nickname `0` — `str()` of a JSON integer happening to
    match the name rule — against a schema that says `string`. Semantic: refuse, naming the
    field, and store nothing."""
    response = client.post("/r/type-check", json={"from": 0, "text": "hello"})
    assert response.status_code == 400
    assert response.text.splitlines()[0] == "400 bad from: must be a string"
    assert client.get("/r/type-check?format=json").json()["messages"] == []
    # The same rule on the other free-form field, and on both POST lanes' shared reader.
    assert client.post("/r/type-check", json={"from": "b", "text": 12345}).status_code == 400
    assert client.post("/kv/plans/k", json={"value": ["x"]}).status_code == 400


def test_373_an_unsigned_post_without_from_names_from_not_the_room(client):
    """A missing `from` became `""` and then failed *room*-name validation, so the 400
    quoted the shared `<room>/<nick>/<ns>/<key>` rule and a caller who had got the room
    right had no way back to the real cause."""
    response = client.post("/r/lobby", json={"text": "hello"})
    assert response.status_code == 400
    assert response.text.splitlines()[0] == "400 bad from: required"
    assert "bad name" not in response.text
    # The signed lane names its author with the DID, so it is unaffected: `from` is
    # required on the unsigned lane only, which is what the body schema's anyOf says.
    body = client.get("/openapi.json").json()["paths"]["/r/{room}"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert schema["anyOf"] == [{"required": ["from"]}, {"required": ["did"]}]

    # A *malformed* `from` was the same failure in a different coat, and the one half the
    # issue's own suggested fix named that its reproduction did not: it reached valid_name
    # as a nick and came back quoting the shared <room>/<nick>/<ns>/<key> rule, so the
    # caller still could not tell which field it had got wrong. All four ways of getting
    # `from` wrong now name `from`.
    malformed = client.post("/r/lobby", json={"from": "Bad Name", "text": "hi"})
    assert malformed.status_code == 400
    assert malformed.text.splitlines()[0].startswith("400 bad from: 'Bad Name' must match")
    assert client.post("/r/lobby", json={"from": 0, "text": "hi"}).text.startswith("400 bad from:")
    assert client.post("/r/lobby", json={"from": "bot", "text": "hi"}).status_code == 200


def test_282_a_capitalised_falsy_if_absent_is_falsy(client):
    """`?if_absent=False` missed the lowercase-only tuple, read as *true*, and turned an
    unconditional overwrite into a 409. Now matched case-insensitively against a stated
    set — and anything outside that set is a 400 naming `if_absent`, not a guess."""
    client.get("/kv/scratch/key1/set/val1")
    for spelling in ("False", "FALSE", "no", "OFF", "0"):
        overwrite = client.get(f"/kv/scratch/key1/set/val2?if_absent={spelling}")
        assert overwrite.status_code == 200, (spelling, overwrite.text)
    for spelling in ("True", "YES", "on", "1"):
        claim = client.get(f"/kv/scratch/key1/set/val3?if_absent={spelling}")
        assert claim.status_code == 409, (spelling, claim.text)
    refused = client.get("/kv/scratch/key1/set/val4?if_absent=maybe")
    assert refused.status_code == 400
    assert refused.text.splitlines()[0].startswith("400 bad if_absent: expected one of")


def test_290_if_together_with_if_absent_is_refused_rather_than_resolved(client):
    """`?if=X&if_absent=1` dropped the `if=` and answered `ok` for a request whose other
    half could not hold — a silent fallback on the compare-and-set gate itself. There is no
    correct pick between "nothing is there" and "exactly this is there", so neither lane
    picks one."""
    refused = client.get("/kv/scratch/both/set/v?if=X&if_absent=1")
    assert refused.status_code == 400
    assert refused.text.splitlines()[0] == (
        "400 bad if_absent: refused with if= — send one condition, not both"
    )
    assert client.get("/kv/scratch/both").status_code == 404  # and nothing was written
    posted = client.post("/kv/scratch/both", json={"value": "v", "if": "X", "if_absent": True})
    assert posted.status_code == 400 and "if=" in posted.text

    # ...but only a *true* `if_absent` contradicts `if=`. A false one is not a second
    # condition, so this stays an ordinary compare-and-set — refusing on the key's mere
    # presence would break every client that serialises the flag it holds rather than
    # omitting it, and it is not what #290 asked for either.
    client.get("/kv/scratch/cas/set/v1")
    assert client.get("/kv/scratch/cas/set/v2?if=v1&if_absent=0").status_code == 200
    assert client.get("/kv/scratch/cas/set/v3?if=v2&if_absent=FALSE").status_code == 200
    # and it is a real compare-and-set, not a condition quietly dropped on the way through
    assert client.get("/kv/scratch/cas/set/v4?if=WRONG&if_absent=0").status_code == 409
    kept = client.post("/kv/scratch/cas", json={"value": "v5", "if": "v3", "if_absent": False})
    assert kept.status_code == 200, kept.text
    assert client.get("/kv/scratch/cas").text.splitlines()[-1] == "v5"
