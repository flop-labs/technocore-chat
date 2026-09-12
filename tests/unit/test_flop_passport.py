"""The local FLOP Passport example's identity and evidence boundaries."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from _client import _keypair
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "flop_passport_app", ROOT / "examples" / "flop_passport" / "app.py"
)
assert SPEC and SPEC.loader
passport = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = passport
SPEC.loader.exec_module(passport)

INDEXER_SPEC = importlib.util.spec_from_file_location(
    "flop_passport_indexer", ROOT / "examples" / "flop_passport" / "indexer.py"
)
assert INDEXER_SPEC and INDEXER_SPEC.loader
indexer = importlib.util.module_from_spec(INDEXER_SPEC)
sys.modules[INDEXER_SPEC.name] = indexer
INDEXER_SPEC.loader.exec_module(indexer)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(passport, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(passport, "DB_PATH", tmp_path / "passport.sqlite3")
    monkeypatch.setattr(passport, "INDEXER_TOKEN", "index-secret")
    yield TestClient(passport.app)


def profile(username="scout", bio="Evidence collector", github="flop-scout"):
    return {
        "username": username,
        "bio": bio,
        "github": github,
        "x_handle": "flop_scout",
        "avatar_url": "https://example.test/avatar.png",
        "kind": "agent",
    }


def challenge(client, did, claimed):
    response = client.post("/api/challenges", json={"did": did, "profile": claimed})
    assert response.status_code == 200
    return response.json()


def verify(client, did, sign, claimed, proof):
    return client.post(
        "/api/verify",
        json={
            "did": did,
            "nonce": proof["nonce"],
            "signature": sign(proof["message"]),
            "profile": claimed,
        },
    )


def test_did_owner_can_create_a_profile_and_replay_is_rejected(client):
    did, sign = _keypair()
    claimed, proof = profile(), challenge(client, did, profile())
    assert verify(client, did, sign, claimed, proof).status_code == 200
    replay = verify(client, did, sign, claimed, proof)
    assert replay.status_code == 401 and "used" in replay.json()["error"]
    public = client.get("/api/profiles/scout").json()
    assert public["did"] == did
    assert public["self_declared"]["github"] == "flop-scout"


def test_challenge_database_does_not_store_the_plaintext_nonce(client):
    did, _sign = _keypair(8)
    proof = challenge(client, did, profile())
    with sqlite3.connect(passport.DB_PATH) as db:
        stored = db.execute("SELECT * FROM challenges").fetchone()
    assert proof["nonce"] not in map(str, stored)


def test_challenge_binds_the_exact_profile_and_owner_can_update_with_a_fresh_one(client):
    did, sign = _keypair(2)
    proof = challenge(client, did, profile())
    changed = profile(bio="Changed after challenge")
    assert verify(client, did, sign, changed, proof).status_code == 401
    fresh = challenge(client, did, changed)
    assert verify(client, did, sign, changed, fresh).status_code == 200
    assert client.get(f"/api/profiles/{did}").json()["self_declared"]["bio"] == changed["bio"]


def test_wrong_key_cannot_claim_another_did(client):
    did, _sign = _keypair(3)
    _other_did, other_sign = _keypair(4)
    proof = challenge(client, did, profile())
    assert verify(client, did, other_sign, profile(), proof).status_code == 403


def test_username_collision_is_a_controlled_conflict(client):
    first_did, first_sign = _keypair(9)
    second_did, second_sign = _keypair(10)
    claimed = profile(username="shared-name")
    assert (
        verify(
            client, first_did, first_sign, claimed, challenge(client, first_did, claimed)
        ).status_code
        == 200
    )
    response = verify(
        client, second_did, second_sign, claimed, challenge(client, second_did, claimed)
    )
    assert response.status_code == 409
    assert response.json() == {"error": "username is already claimed"}


def test_contribution_ingestion_is_authenticated_and_idempotent(client):
    did, _sign = _keypair(5)
    item = {
        "did": did,
        "source": "github",
        "source_id": "flop-labs/technocore-chat:pr:496",
        "kind": "pull_request",
        "title": "Make room generation atomic",
        "url": "https://github.com/flop-labs/technocore-chat/pull/496",
        "occurred_at": 1,
        "evidence": {"repo": "flop-labs/technocore-chat", "number": 496, "state": "merged"},
    }
    assert client.post("/api/contributions/ingest", json={"items": [item]}).status_code == 404
    headers = {"Authorization": "Bearer index-secret"}
    first = client.post("/api/contributions/ingest", headers=headers, json={"items": [item]}).json()
    second = client.post(
        "/api/contributions/ingest", headers=headers, json={"items": [item]}
    ).json()
    assert first == {"accepted": 1, "inserted": 1, "updated": 0}
    assert second == {"accepted": 1, "inserted": 0, "updated": 0}


def test_github_reingestion_moves_attribution_to_corrected_did(client):
    first_did, first_sign = _keypair(18)
    second_did, second_sign = _keypair(19)
    first = profile(username="mapped-first", github="alice")
    second = profile(username="mapped-second", github="alice")
    assert (
        verify(
            client, first_did, first_sign, first, challenge(client, first_did, first)
        ).status_code
        == 200
    )
    assert (
        verify(
            client, second_did, second_sign, second, challenge(client, second_did, second)
        ).status_code
        == 200
    )
    item = {
        "did": first_did,
        "source": "github",
        "source_id": "owner/repo:pr:42",
        "kind": "pull_request",
        "title": "Mapped contribution",
        "occurred_at": 42,
        "evidence": {"repo": "owner/repo", "number": 42, "state": "merged"},
    }
    headers = {"Authorization": "Bearer index-secret"}
    first_ingest = client.post("/api/contributions/ingest", headers=headers, json={"items": [item]})
    item["did"] = second_did
    item["title"] = "Refreshed contribution"
    item["evidence"]["api_url"] = "https://api.github.com/repos/owner/repo/pulls/42"
    second_ingest = client.post(
        "/api/contributions/ingest", headers=headers, json={"items": [item]}
    )

    assert first_ingest.json() == {"accepted": 1, "inserted": 1, "updated": 0}
    assert second_ingest.json() == {"accepted": 1, "inserted": 0, "updated": 1}
    assert client.get("/api/profiles/mapped-first").json()["verified"]["counts"]["total"] == 0
    corrected = client.get("/api/profiles/mapped-second").json()["verified"]
    assert corrected["counts"]["total"] == 1
    assert corrected["counts"]["merged_prs"] == 1
    assert corrected["contributions"][0]["title"] == "Refreshed contribution"
    assert corrected["contributions"][0]["evidence"]["api_url"].endswith("/pulls/42")


def test_unmerged_pull_requests_do_not_count_as_merged_or_unlock_shipper(client):
    did, sign = _keypair(11)
    claimed = profile(username="not-shipped")
    assert verify(client, did, sign, claimed, challenge(client, did, claimed)).status_code == 200
    items = [
        {
            "did": did,
            "source": "github",
            "source_id": f"repo:pr:{number}",
            "kind": "pull_request",
            "title": f"Open PR {number}",
            "occurred_at": number,
            "evidence": {"repo": "owner/repo", "number": number, "state": "open"},
        }
        for number in range(1, 4)
    ]
    response = client.post(
        "/api/contributions/ingest",
        headers={"Authorization": "Bearer index-secret"},
        json={"items": items},
    )
    assert response.status_code == 200
    verified = client.get("/api/profiles/not-shipped").json()["verified"]
    assert verified["counts"]["merged_prs"] == 0
    assert "shipper" not in {badge["id"] for badge in verified["badges"]}


def test_github_handle_search_returns_all_matches_but_profile_route_is_not_ambiguous(client):
    first_did, first_sign = _keypair(12)
    second_did, second_sign = _keypair(13)
    first = profile(username="alice-one", github="shared-github")
    second = profile(username="alice-two", github="shared-github")
    assert (
        verify(
            client, first_did, first_sign, first, challenge(client, first_did, first)
        ).status_code
        == 200
    )
    assert (
        verify(
            client, second_did, second_sign, second, challenge(client, second_did, second)
        ).status_code
        == 200
    )
    results = client.get("/api/search?q=shared-github").json()["results"]
    assert {row["did"] for row in results} == {first_did, second_did}
    assert client.get("/api/profiles/shared-github").status_code == 404


def test_github_indexer_reads_an_eligible_contribution_beyond_page_one(monkeypatch):
    did, _sign = _keypair(14)
    ignored = {
        "number": 1,
        "title": "Not mapped",
        "merged_at": "2026-01-01T00:00:00Z",
        "user": {"login": "someone-else"},
    }
    eligible = {
        "number": 101,
        "title": "Mapped on page two",
        "merged_at": "2026-01-02T00:00:00Z",
        "html_url": "https://github.com/owner/repo/pull/101",
        "url": "https://api.github.com/repos/owner/repo/pulls/101",
        "user": {"login": "alice"},
    }

    def fake_fetch(url, _token=""):
        if url == "https://api.github.com/repos/owner/repo":
            return {"full_name": "owner/repo"}
        if "/issues?" in url:
            return []
        return [ignored] * 100 if url.endswith("page=1") else [eligible]

    monkeypatch.setattr(indexer, "fetch_json", fake_fetch)
    items = indexer.github_items("owner/repo", {"alice": did}, "")
    assert [item["source_id"] for item in items] == ["owner/repo:pr:101"]


def test_github_repository_spelling_uses_canonical_identity(client, monkeypatch):
    did, sign = _keypair(20)
    claimed = profile(username="canonical-repo", github="alice")
    assert verify(client, did, sign, claimed, challenge(client, did, claimed)).status_code == 200
    pull = {
        "number": 42,
        "title": "Canonical repository",
        "merged_at": "2026-01-02T00:00:00Z",
        "html_url": "https://github.com/flop-labs/technocore-chat/pull/42",
        "url": "https://api.github.com/repos/flop-labs/technocore-chat/pulls/42",
        "user": {"login": "alice"},
    }

    def fake_fetch(url, _token=""):
        if "?" not in url:
            return {"full_name": "flop-labs/technocore-chat"}
        if "/issues?" in url:
            return []
        return [pull]

    monkeypatch.setattr(indexer, "fetch_json", fake_fetch)
    lower = indexer.github_items("flop-labs/technocore-chat", {"alice": did}, "")
    upper = indexer.github_items("FLOP-LABS/TECHNOCORE-CHAT", {"alice": did}, "")
    assert lower == upper
    assert lower[0]["source_id"] == "flop-labs/technocore-chat:pr:42"
    assert lower[0]["evidence"]["repo"] == "flop-labs/technocore-chat"

    headers = {"Authorization": "Bearer index-secret"}
    first = client.post("/api/contributions/ingest", headers=headers, json={"items": lower})
    second = client.post("/api/contributions/ingest", headers=headers, json={"items": upper})
    assert first.json()["inserted"] == 1
    assert second.json() == {"accepted": 1, "inserted": 0, "updated": 0}
    assert client.get("/api/profiles/canonical-repo").json()["verified"]["counts"]["total"] == 1


def test_indexer_chunks_101_items_before_checkpointing_cursor(client, monkeypatch):
    payloads = []
    did, sign = _keypair(17)
    claimed = profile(username="batch-user")
    assert verify(client, did, sign, claimed, challenge(client, did, claimed)).status_code == 200

    def local_post(_base, token, payload):
        assert token == "index-secret"
        payloads.append(payload)
        body = indexer._encoded_ingest(payload)
        assert len(body) <= indexer.MAX_INGEST_BODY
        response = client.post(
            "/api/contributions/ingest",
            headers={"Authorization": "Bearer index-secret"},
            content=body,
        )
        assert response.status_code == 200
        return response.json()

    monkeypatch.setattr(indexer, "_post_payload", local_post)
    items = [
        {
            "did": did,
            "source": "github",
            "source_id": f"owner/repo:issue:{number}",
            "kind": "issue",
            "title": f"Issue {number}",
            "occurred_at": number,
            "evidence": {"repo": "owner/repo", "number": number, "state": "open"},
        }
        for number in range(101)
    ]
    result = indexer.post_batch(
        "http://127.0.0.1:8090",
        "index-secret",
        "github",
        "next-cursor",
        items,
    )

    assert [len(payload["items"]) for payload in payloads] == [100, 1, 0]
    assert all("cursor" not in payload for payload in payloads[:-1])
    assert payloads[-1] == {"source": "github", "cursor": "next-cursor", "items": []}
    assert result == {"accepted": 101, "inserted": 101}
    verified = client.get("/api/profiles/batch-user").json()["verified"]
    assert verified["counts"]["total"] == 101
    with passport.database() as db:
        assert (
            db.execute("SELECT cursor FROM indexer_state WHERE source='github'").fetchone()[
                "cursor"
            ]
            == "next-cursor"
        )


def test_indexer_chunks_by_encoded_body_size(monkeypatch):
    bodies = []

    class Response:
        def __init__(self, accepted):
            self.accepted = accepted

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"accepted": self.accepted, "inserted": self.accepted}).encode()

    def fake_open(request, timeout):
        assert timeout == 20
        bodies.append(request.data)
        return Response(len(json.loads(request.data)["items"]))

    monkeypatch.setattr(indexer.urllib.request, "urlopen", fake_open)
    indexer.post_batch(
        "http://127.0.0.1:8090",
        "index-secret",
        "technocore",
        "201",
        [{"evidence": "x" * 20_000}, {"evidence": "y" * 20_000}],
    )

    assert [len(json.loads(body)["items"]) for body in bodies] == [1, 1, 0]
    assert all(len(body) <= indexer.MAX_INGEST_BODY for body in bodies)


def test_technocore_indexer_recovers_a_signed_record_hidden_by_the_tail_window(monkeypatch):
    did, _sign = _keypair(15)
    unsigned = [
        {
            "seq": seq,
            "from": "visitor",
            "text": "ordinary traffic",
            "ts": "2026-01-01T00:00:00Z",
        }
        for seq in range(102, 302)
    ]
    signed = {
        "seq": 101,
        "from": did,
        "nonce": 9,
        "text": "signed contribution",
        "ts": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        indexer,
        "fetch_json",
        lambda _url: {"first_seq": 102, "last_seq": 301, "messages": unsigned},
    )
    monkeypatch.setattr(indexer, "fetch_jsonl", lambda _url: [signed, *unsigned])
    items, cursor = indexer.technocore_items("https://technocore.chat", "technocore", 100)
    assert cursor == 301
    assert [item["source_id"] for item in items] == ["https://technocore.chat:technocore:101"]


def test_technocore_indexer_recovers_retained_history_on_first_run(monkeypatch):
    did, _sign = _keypair(16)
    signed = {
        "seq": 1,
        "from": did,
        "nonce": 1,
        "text": "retained before first index",
        "ts": "2026-01-01T00:00:00Z",
    }
    unsigned = [
        {
            "seq": seq,
            "from": "visitor",
            "text": "ordinary traffic",
            "ts": "2026-01-01T00:00:00Z",
        }
        for seq in range(2, 202)
    ]
    monkeypatch.setattr(
        indexer,
        "fetch_json",
        lambda _url: {"first_seq": 2, "last_seq": 201, "messages": unsigned},
    )
    monkeypatch.setattr(indexer, "fetch_jsonl", lambda _url: [signed, *unsigned])
    items, cursor = indexer.technocore_items("https://technocore.chat", "technocore", 0)
    assert cursor == 201
    assert [item["source_id"] for item in items] == ["https://technocore.chat:technocore:1"]


def test_technocore_server_spelling_shares_identity_and_cursor_namespace(client, monkeypatch):
    did, sign = _keypair(21)
    claimed = profile(username="canonical-server")
    assert verify(client, did, sign, claimed, challenge(client, did, claimed)).status_code == 200
    signed = {
        "seq": 123,
        "from": did,
        "nonce": 7,
        "text": "one durable source",
        "ts": "2026-01-01T00:00:00Z",
    }
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return {"first_seq": 123, "last_seq": 123, "messages": [signed]}

    monkeypatch.setattr(indexer, "fetch_json", fake_fetch)
    plain, _cursor = indexer.technocore_items("https://technocore.chat", "technocore", 122)
    trailing, _cursor = indexer.technocore_items("https://technocore.chat/", "technocore", 122)
    assert plain == trailing
    assert fetched == [
        "https://technocore.chat/r/technocore?format=json&since=122&limit=200",
        "https://technocore.chat/r/technocore?format=json&since=122&limit=200",
    ]
    assert indexer.technocore_state_key(
        "https://technocore.chat", "technocore"
    ) == indexer.technocore_state_key("https://technocore.chat/", "technocore")

    headers = {"Authorization": "Bearer index-secret"}
    first = client.post("/api/contributions/ingest", headers=headers, json={"items": plain})
    second = client.post("/api/contributions/ingest", headers=headers, json={"items": trailing})
    assert first.json()["inserted"] == 1
    assert second.json() == {"accepted": 1, "inserted": 0, "updated": 0}
    verified = client.get("/api/profiles/canonical-server").json()["verified"]
    assert verified["counts"]["technocore"] == 1


def test_signed_technocore_evidence_is_verified(client):
    did, sign = _keypair(6)
    evidence = {"room": "technocore", "nonce": 9, "text": "measured the network"}
    item = {
        "did": did,
        "source": "technocore",
        "source_id": "technocore:91",
        "kind": "signed_post",
        "title": evidence["text"],
        "occurred_at": 2,
        "evidence": evidence | {"signature": sign("technocore|9|measured the network")},
    }
    response = client.post(
        "/api/contributions/ingest",
        headers={"Authorization": "Bearer index-secret"},
        json={"items": [item]},
    )
    assert response.status_code == 200 and response.json()["inserted"] == 1
    item["source_id"] = "technocore:92"
    item["evidence"]["text"] = "tampered"
    assert (
        client.post(
            "/api/contributions/ingest",
            headers={"Authorization": "Bearer index-secret"},
            json={"items": [item]},
        ).status_code
        == 400
    )


def test_public_page_is_static_csp_pinned_and_search_returns_safe_json(client):
    did, sign = _keypair(7)
    unsafe = profile(username="safe-name", bio="<img src=x onerror=alert(1)>")
    proof = challenge(client, did, unsafe)
    assert verify(client, did, sign, unsafe, proof).status_code == 200
    page = client.get("/")
    assert (
        page.status_code == 200 and "default-src 'none'" in page.headers["content-security-policy"]
    )
    assert unsafe["bio"] not in page.text
    result = client.get("/api/search?q=safe-name").json()["results"][0]
    assert result["bio"] == unsafe["bio"]
