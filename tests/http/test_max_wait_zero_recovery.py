"""Regression test for #759: CHAT_MAX_WAIT=0 must not recommend &wait=0."""

import time

import pytest
from starlette.testclient import TestClient

import app as app_module
import config
import limit
import store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Local replica of tests/_client.py:client so this file is hermetic."""
    origin = time.monotonic()
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int((now - origin) // ttl))
    app_module._buckets.clear()
    app_module._rooms_walk.cache_clear()
    store._cached_window.cache_clear()
    store._topics_memo.cache_clear()
    app_module._identities.clear()
    app_module._proxy_evidence["proxied_requests"] = 0
    limit._dupes.clear()
    with config.override(ROOT=tmp_path, DUPE_FILTER_SECONDS=0):
        yield TestClient(app_module.app)


def test_zero_max_wait_recovery_text_does_not_recommend_zero_wait(client, monkeypatch):
    """When CHAT_MAX_WAIT=0 disables long-polling, the 429 body must not
    advise agents to prefer &wait=0 (zero-delay tight polling).

    The contract has three halves:
      1. the zero-delay advice is absent,
      2. the useful `since=<last seq>` guidance remains,
      3. a positive CHAT_MAX_WAIT keeps the existing long-poll advice.
    """
    # --- Half 1+2: CHAT_MAX_WAIT=0 ---
    with config.override(RATE_READ=1, MAX_WAIT=0):
        app_module._buckets.clear()
        first = client.get("/r/lobby")
        assert first.status_code == 200
        refused = client.get("/r/lobby")
        assert refused.status_code == 429
        # Must not recommend zero-delay tight polling.
        assert "prefer &wait=0" not in refused.text, (
            "429 body still recommends &wait=0 when CHAT_MAX_WAIT=0"
        )
        assert "one request per 0s" not in refused.text, (
            "429 body still says 'one request per 0s' when CHAT_MAX_WAIT=0"
        )
        # Must still give the useful retry guidance.
        assert "cheaper pattern:" in refused.text
        assert "since=<last seq you saw>" in refused.text
        assert "retry after:" in refused.text

    # --- Half 3: positive CHAT_MAX_WAIT keeps the old advice ---
    with config.override(RATE_READ=1, MAX_WAIT=10):
        app_module._buckets.clear()
        first = client.get("/r/lobby")
        assert first.status_code == 200
        refused = client.get("/r/lobby")
        assert refused.status_code == 429
        assert "prefer &wait=10 to tight polling" in refused.text
        assert "one request per 10s" in refused.text
