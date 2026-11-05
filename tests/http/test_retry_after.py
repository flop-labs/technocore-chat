"""Following a 429's own delay must allow the next uncontended request."""

from types import SimpleNamespace

import _client
import pytest

import limit

client = _client.client


@pytest.mark.parametrize("kind", ["read", "write", "create"])
def test_following_retry_after_does_not_return_another_429(client, monkeypatch, kind):
    import config

    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(limit, "time", SimpleNamespace(monotonic=lambda: now.value))
    ip = "192.0.2.1"
    headers = {"x-test-ip": ip}
    with config.override(
        RATE_READ=2, RATE_WRITE=2, RATE_ROOMS_PER_DAY=7, CLIENT_IP_HEADER="x-test-ip"
    ):
        limit._buckets[(ip, kind)] = (0.25, now.value)

        def attempt():
            if kind == "read":
                return client.get("/r/retry-delay", headers=headers)
            return client.post(
                "/r/retry-delay", headers=headers, json={"from": "bot", "text": "hello"}
            )

        refused = attempt()
        assert refused.status_code == 429
        assert (
            "room-creation budget" if kind == "create" else f"the {kind} budget"
        ) in refused.text
        advertised = int(refused.headers["retry-after"])
        assert f"retry after: {advertised}s" in refused.text

        # No other caller, sleep, or real-time scheduling: obey exactly the advertised delay.
        now.value += advertised
        retried = attempt()
        assert retried.status_code == 200, retried.text
        assert advertised == (9258 if kind == "create" else 23)
