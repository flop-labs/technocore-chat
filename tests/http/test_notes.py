"""Run: uv run --group dev python -m pytest tests"""

import _client
import pytest
from _client import (
    _keypair,
    _post_signed,
    _say_signed,
)

client = _client.client  # the shared TestClient fixture


def test_notes_roundtrip(client):
    assert client.get("/kv/plans/next/set/ship%20the%20thing").status_code == 200
    assert "ship the thing" in client.get("/kv/plans/next").text
    assert "/kv/plans/next" in client.get("/kv/plans").text
    assert client.get("/kv/plans/missing").status_code == 404


def test_post_lane(client):
    import app as app_module

    r = client.post("/r/lobby", json={"from": "carol", "text": "via post"})
    assert r.status_code == 200 and "via post" in r.text
    assert client.post("/r/lobby", content=b"x" * (app_module.MAX_BODY + 1)).status_code == 413


def test_post_lane_reports_write_budget_like_get_writes(client, monkeypatch):
    """POST pays the same write bucket as GET /say, so it must carry the same in-body
    budget hint for clients whose harness does not expose response headers.
    """
    import config

    with config.override(RATE_WRITE=4):
        responses = [
            client.post("/r/lobby", json={"from": "bot", "text": f"m{i}"}) for i in range(4)
        ]
        assert [response.status_code for response in responses] == [200, 200, 200, 200]
        assert "# budget: 0 of 4 writes left" in responses[-1].text


def test_a_lost_conditional_write_carries_the_value_after_the_first_line(client):
    """The manual promises a 409 lets you rebase without re-reading, and the page's tool
    lane stopped truncating error bodies so write_note can keep that promise. Pin where
    the value actually is: the first line is the sentence, the value is the last line.
    """
    client.get("/kv/plans/next/set/world")
    lost = client.get("/kv/plans/next/set/nope?if=stale")
    assert lost.status_code == 409
    lines = lost.text.rstrip("\n").split("\n")
    assert lines[0].startswith("409") and "world" not in lines[0]
    assert lines[-1] == "world"


def test_webmcp_tool_results_carry_the_whole_server_reply(client):
    """A one-line squeeze used to live in the tool lane, and it dropped the value a 409
    carries. The status badge above still takes a first line — it has one line to render —
    so this is asserted at the tool lane rather than page-wide.
    """
    body = client.get("/humans").text
    assert "throw new Error(body.trim()" in body
    assert "function noteValue(body)" in body
    assert ".then(function (body) { return result(noteValue(body)); })" in body


def test_notes_have_a_post_lane_so_their_documented_cap_is_reachable(client):
    """8192 characters URL-encode past the request line (and past Cloudflare's 16 KiB URL
    ceiling), so POST must accept the full character limit in every JSON encoding."""
    import json

    import store

    for label, ch in (("ascii", "z"), ("emoji", "\U0001f600")):
        value = ch * store.MAX_VALUE_CHARS
        escaped = json.dumps({"value": value}, ensure_ascii=True)
        r = client.post(
            f"/kv/plans/big-{label}",
            content=escaped.encode(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 200, (label, len(escaped), r.text[:200])
        assert client.get(f"/kv/plans/big-{label}").text.count(ch) == store.MAX_VALUE_CHARS
    assert (
        client.post(
            "/kv/plans/toobig", json={"value": "z" * (store.MAX_VALUE_CHARS + 1)}
        ).status_code
        == 400
    )


def test_full_length_conditional_note_is_postable_with_escaped_json(client):
    """A valid CAS body carries two full notes: the replacement and the value last read."""
    import json

    import store

    previous = "\U0001f600" * store.MAX_VALUE_CHARS
    replacement = "\U0001f680" * store.MAX_VALUE_CHARS
    assert client.post("/kv/plans/cas-max", json={"value": previous}).status_code == 200

    escaped = json.dumps({"value": replacement, "if": previous}, ensure_ascii=True)
    r = client.post(
        "/kv/plans/cas-max",
        content=escaped.encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200, (len(escaped), r.text[:200])
    assert client.get("/kv/plans/cas-max").text.count("\U0001f680") == store.MAX_VALUE_CHARS


def test_cas_rejects_a_write_whose_read_went_stale(client):
    client.get("/kv/coord/leader/set/none")
    # Both agents read "none"; both try to claim. Exactly one may win.
    first = client.get("/kv/coord/leader/set/agent-a?if=none")
    second = client.get("/kv/coord/leader/set/agent-b?if=none")
    assert first.status_code == 200
    assert second.status_code == 409
    assert "agent-a" in client.get("/kv/coord/leader").text  # loser did not clobber
    assert "agent-a" in second.text  # 409 hands back the current value to rebase on


def test_if_absent_creates_exactly_once(client):
    assert client.get("/kv/coord/claim/set/agent-a?if_absent=1").status_code == 200
    assert client.get("/kv/coord/claim/set/agent-b?if_absent=1").status_code == 409
    assert "agent-a" in client.get("/kv/coord/claim").text


def test_cas_distinguishes_absent_from_empty_and_works_over_post(client):
    # An empty string is a legal value, so absence cannot be encoded as if=<empty>.
    assert client.post("/kv/coord/n", json={"value": "0", "if_absent": True}).status_code == 200
    r = client.post("/kv/coord/n", json={"value": "1", "if": "0"})
    assert r.status_code == 200
    assert client.post("/kv/coord/n", json={"value": "2", "if": "0"}).status_code == 409
    assert "1" in client.get("/kv/coord/n").text


def test_unconditional_write_still_overwrites(client):
    client.get("/kv/coord/plain/set/one")
    assert client.get("/kv/coord/plain/set/two").status_code == 200  # no condition, no conflict
    assert "two" in client.get("/kv/coord/plain").text


def test_newlines_are_flattened_in_both_write_lanes(client):
    """llms.txt used to promise POST carried multi-line text. It never did."""
    client.post("/r/lobby", json={"from": "bot", "text": "line1\nline2\r\nline3"})
    # one space per stripped character, so CRLF leaves two — nothing is silently merged
    assert "line1 line2  line3" in client.get("/r/lobby").text
    assert client.get("/r/lobby/say/bot/a%0Ab").status_code == 404  # not routable in a path
    manual = client.get("/llms.txt").text
    assert "no multi-line message" in manual


def test_full_length_signed_message_is_postable_with_escaped_json(client):
    import json

    import store

    room = "signed-max"
    nonce = 9_223_372_036_854_775_807
    text_value = "\U0001f600" * store.MAX_TEXT_CHARS
    did, sign = _keypair()
    payload = {
        "did": did,
        "sig": sign(f"{room}|{nonce}|{text_value}"),
        "nonce": str(nonce),
        "text": text_value,
    }
    escaped = json.dumps(payload, ensure_ascii=True)
    r = client.post(
        f"/r/{room}",
        content=escaped.encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200, (len(escaped), r.text[:200])
    stored = client.get(f"/r/{room}?format=json").json()["messages"][0]
    assert stored["from"] == did and stored["nonce"] == nonce and stored["text"] == text_value


def test_a_signed_write_is_attributed_to_the_key_not_a_nickname(client):
    did, sign = _keypair()
    r = _say_signed(client, "lobby", did, sign, "signed hello")
    assert r.status_code == 200
    view = client.get("/r/lobby?format=json").json()
    assert view["messages"][0]["from"] == did  # json carries the DID in full
    assert view["messages"][0]["nonce"] == 1
    # the text view abbreviates: 56 base58 characters per line would be the whole budget
    body = client.get("/r/lobby").text
    assert f"<{did[len('did:key:') :][:4]}…{did[-4:]}> signed hello" in body
    assert did not in body


def test_signed_writes_fail_closed_on_every_malformed_credential(client):
    did, sign = _keypair()
    other, _ = _keypair(seed=2)
    good = sign("lobby|1|hi")
    assert client.get(f"/r/lobby/say-signed/{did}/{good}/1/hi").status_code == 200
    # a valid signature from a different key
    assert client.get(f"/r/lobby/say-signed/{other}/{good}/2/hi").status_code == 403
    # a signature over different text
    assert client.get(f"/r/lobby/say-signed/{did}/{sign('lobby|2|other')}/2/hi").status_code == 403
    # a signature over a different room: room is inside the signed string for this reason
    assert client.get(f"/r/other/say-signed/{did}/{sign('lobby|2|hi')}/2/hi").status_code == 403
    # malformed dids and signatures never reach the verifier
    for bad_did in ("did:key:zNotAKey", "did:web:example.com", "z6Mk", "did:key:" + "z" * 48):
        assert client.get(f"/r/lobby/say-signed/{bad_did}/{good}/9/hi").status_code == 400, bad_did
    for bad_sig in ("x", good[:-1], good + "AA", good.replace("_", "+")):
        assert client.get(f"/r/lobby/say-signed/{did}/{bad_sig}/9/hi").status_code in (400, 403)
    for bad_nonce in ("abc", "-1", "1.5", "9" * 20):
        assert client.get(f"/r/lobby/say-signed/{did}/{good}/{bad_nonce}/hi").status_code == 400
    assert client.get("/r/lobby?format=json").json()["count"] == 1  # only the good one landed


def test_a_replayed_signed_url_is_refused_while_the_message_is_still_there(client):
    did, sign = _keypair()
    url = f"/r/lobby/say-signed/{did}/{sign('lobby|7|once')}/7/once"
    assert client.get(url).status_code == 200
    r = client.get(url)  # the identical captured URL, again
    assert r.status_code == 400 and "not greater than 7" in r.text
    assert (
        client.get(f"/r/lobby/say-signed/{did}/{sign('lobby|6|older')}/6/older").status_code == 400
    )
    assert client.get(f"/r/lobby/say-signed/{did}/{sign('lobby|8|next')}/8/next").status_code == 200
    assert client.get("/r/lobby?format=json").json()["count"] == 2


def test_a_did_quoted_in_another_agents_text_is_not_that_agents_nonce(client):
    """`_last_nonce` rejects lines on bytes before parsing them, and a DID may legally
    appear in a *message* — an agent addressing another by name. Only `from` is that
    agent's nonce: a mention that matched on bytes must fall through the full parse and
    count for nothing, or one agent's counter would gate another's writes.
    """
    talker, talker_sign = _keypair(seed=1)
    quoted, quoted_sign = _keypair(seed=2)
    # a signed record carrying a *high* nonce whose text names the other agent's DID
    assert (
        _post_signed(
            client, "lobby", talker, talker_sign, f"@{quoted} ready when you are", nonce=9000
        ).status_code
        == 200
    )
    # the quoted agent has never written here, so its own counter is untouched by that
    assert (
        _post_signed(client, "lobby", quoted, quoted_sign, "on my way", nonce=5).status_code == 200
    )
    # and its own record still governs: the replay is refused against 5, not against 9000
    replay = _post_signed(client, "lobby", quoted, quoted_sign, "on my way", nonce=5)
    assert replay.status_code == 400 and "not greater than 5" in replay.text
    assert _post_signed(client, "lobby", quoted, quoted_sign, "again", nonce=6).status_code == 200


def test_the_signature_covers_the_swept_text_not_the_raw_text(client):
    """Both directions, so the contract is unambiguous: what is stored is what was signed.

    A record whose signature covered pre-sweep bytes could never be re-verified from the
    room, because the pre-sweep bytes are exactly what the store refuses to keep.
    """
    import store

    did, sign = _keypair()
    raw = "hi\u200bthere"  # a zero-width space the sweep turns into a plain space
    swept = store.clean_text(raw)
    assert swept == "hi there"
    signed_raw = sign(f"lobby|1|{raw}")
    assert client.get(f"/r/lobby/say-signed/{did}/{signed_raw}/1/{raw}").status_code == 403
    signed_swept = sign(f"lobby|1|{swept}")
    assert client.get(f"/r/lobby/say-signed/{did}/{signed_swept}/1/{raw}").status_code == 200
    assert client.get("/r/lobby?format=json").json()["messages"][0]["text"] == swept


def test_the_signed_lane_also_works_over_post(client):
    did, sign = _keypair()
    r = client.post(
        "/r/lobby",
        json={"did": did, "sig": sign("lobby|3|via post"), "nonce": "3", "text": "via post"},
    )
    assert r.status_code == 200
    assert client.get("/r/lobby?format=json").json()["messages"][0]["from"] == did
    bad = client.post(
        "/r/lobby", json={"did": did, "sig": sign("lobby|4|x"), "nonce": "4", "text": "y"}
    )
    assert bad.status_code == 403


def test_signed_post_rejects_padding_and_replays_without_appending(client):
    did, sign = _keypair()
    signature = sign("lobby|7|once")

    # The wire format is exactly 86 unpadded base64url characters. The verifier used to
    # accept padding even though every published description says it is invalid.
    for bad_sig in ("not-a-signature", signature + "=", signature + "=="):
        r = client.post(
            "/r/lobby",
            json={"did": did, "sig": bad_sig, "nonce": "7", "text": "once"},
        )
        assert r.status_code == 400
    assert client.get("/r/lobby?format=json").json()["count"] == 0

    assert _post_signed(client, "lobby", did, sign, "once", nonce=7).status_code == 200
    replay = _post_signed(client, "lobby", did, sign, "replay", nonce=7)
    assert replay.status_code == 400 and "not greater than 7" in replay.text
    assert _post_signed(client, "lobby", did, sign, "older", nonce=6).status_code == 400
    assert _post_signed(client, "lobby", did, sign, "next", nonce=8).status_code == 200
    assert client.get("/r/lobby?format=json").json()["count"] == 2


def test_a_did_with_a_non_base58_character_fails_closed_and_names_the_encoding(client):
    """Prefix and length checks are not enough: characters such as 0/O/I/l are outside
    base58btc. A malformed identity must never fall back to the unsigned lane.
    """
    did, sign = _keypair()
    malformed = did[:-1] + "0"
    signature = sign("lobby|1|hello")
    response = client.get(f"/r/lobby/say-signed/{malformed}/{signature}/1/hello")
    assert response.status_code == 400
    assert "not base58btc" in response.text
    assert client.get("/r/lobby?format=json").json()["count"] == 0


def test_signed_post_covers_the_swept_text_not_the_raw_text(client):
    import store

    did, sign = _keypair()
    raw = "alpha\n\r\tbeta \u200b\u200cgamma"
    swept = store.clean_text(raw)

    good = client.post(
        "/r/lobby?format=json",
        json={"did": did, "sig": sign(f"lobby|1|{swept}"), "nonce": "1", "text": raw},
    )
    assert good.status_code == 200 and good.json()["posted"]["text"] == swept

    signed_raw = client.post(
        "/r/lobby",
        json={"did": did, "sig": sign(f"lobby|2|{raw}"), "nonce": "2", "text": raw},
    )
    assert signed_raw.status_code == 403
    assert client.get("/r/lobby?format=json").json()["count"] == 1


def test_an_unsigned_nick_can_never_look_verified(client):
    """`from` is the provenance field, so the unsigned lane must not be able to reach the
    DID shape — the name allowlist rejects ':' and that is what keeps the lanes apart."""
    import store

    assert client.get("/r/lobby/say/did:key:z6Mkfake/hi").status_code in (400, 404)
    with pytest.raises(store.StoreError):
        store.valid_name("did:key:z6Mkfake")


def test_signed_writes_pay_the_write_budget_like_any_other(client, monkeypatch):
    import config

    with config.override(RATE_WRITE=2):
        did, sign = _keypair()
        codes = [
            _say_signed(client, "lobby", did, sign, f"m{i}", nonce=i).status_code for i in (1, 2, 3)
        ]
        assert codes == [200, 200, 429]
