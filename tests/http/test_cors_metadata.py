"""Allowed browser callers need the metadata carried only in response headers."""

import _client
import pytest
from starlette.middleware.cors import CORSMiddleware

import app as app_module
import config

client = _client.client
ORIGIN = "https://browser.example"


@pytest.fixture
def cors_client(client, monkeypatch):
    # Rebuild the real middleware with the origins a deployment would configure at boot.
    # Leave every other option from app.py intact, including the headers it exposes.
    cors = next(m for m in app_module.app.user_middleware if m.cls is CORSMiddleware)

    def configure(origins):
        monkeypatch.setitem(cors.kwargs, "allow_origins", origins)
        monkeypatch.setattr(app_module.app, "middleware_stack", None)
        return client

    return configure


@pytest.mark.parametrize("origins", [[ORIGIN], ["*"]])
@pytest.mark.parametrize("exists", [False, True])
def test_allowed_export_exposes_its_generation(cors_client, origins, exists):
    browser = cors_client(origins)
    if exists:
        assert browser.get("/r/cors-export/say/bot/hello").status_code == 200
    response = browser.get("/r/cors-export/export", headers={"Origin": ORIGIN})
    same_origin = browser.get("/r/cors-export/export")
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origins[0]
    assert response.content == same_origin.content
    assert response.headers["x-room-generation"] == same_origin.headers["x-room-generation"]
    assert int(response.headers["x-room-generation"]) == int(exists)
    exposed = response.headers.get("access-control-expose-headers", "").lower().split(", ")
    assert "x-room-generation" in exposed
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("lane", ["read", "write", "create"])
def test_allowed_refusal_exposes_its_retry_delay(cors_client, lane):
    browser = cors_client([ORIGIN])
    with config.override(
        RATE_READ=1, RATE_WRITE=1 if lane == "write" else 10, RATE_ROOMS_PER_DAY=1
    ):
        if lane == "read":
            first, second = "/r/never/export", "/r/never/export"
        elif lane == "write":
            first, second = "/kv/cors/key/set/one", "/kv/cors/key/set/two"
        else:
            first, second = "/r/first/say/bot/one", "/r/second/say/bot/two"
        assert browser.get(first, headers={"Origin": ORIGIN}).status_code == 200
        response = browser.get(second, headers={"Origin": ORIGIN})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert response.headers["access-control-allow-origin"] == ORIGIN
    exposed = response.headers.get("access-control-expose-headers", "").lower().split(", ")
    assert "retry-after" in exposed
    assert "x-room-generation" in exposed
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("origins", [[], ["https://someone-else.example"]])
def test_exposed_metadata_does_not_allow_a_disallowed_origin(cors_client, origins):
    response = cors_client(origins).get("/r/never/export", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers["x-room-generation"] == "0"
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_same_origin_export_keeps_its_metadata_without_cors(cors_client):
    response = cors_client([]).get("/r/never/export")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-room-generation"] == "0"
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-expose-headers" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_still_refuses_unlisted_origins_and_credentials(cors_client):
    browser = cors_client([ORIGIN])
    for origin, status in ((ORIGIN, 200), ("https://someone-else.example", 400)):
        response = browser.options(
            "/r/never/export",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert response.status_code == status
        assert "access-control-allow-credentials" not in response.headers
        assert ("access-control-allow-origin" in response.headers) is (status == 200)
