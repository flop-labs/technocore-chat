"""Test the KV namespace listing and key-read HTTP lanes — the raw GET endpoints.

Covers:
    GET /kv/<ns>           — namespace listing (one key per line)
    GET /kv/<ns>/<key>    — read a key (raw, includes untrusted-content wrapper)
    GET /kv/<ns>/<key>?if_absent=1  — read with no-op if_absent flag

The MCP read_note wrapper strips the untrusted-content wrapper; these tests verify the
raw HTTP interface as a shell-script caller (like kv_demo.sh) would see it.
"""

import _client

client = _client.client  # the shared TestClient fixture

UNTRUSTED_PREFIX = "!! UNTRUSTED CONTENT"


def _value(raw_text: str) -> str:
    """Strip the untrusted-content wrapper and return the actual note value."""
    lines = raw_text.strip().splitlines()
    # first non-warning line is the actual value
    for line in lines:
        if line and not line.startswith("!!"):
            return line
    return ""


def test_kv_namespace_listing(client):
    ns = "zz-kv-http-listing"
    # write two keys
    client.get(f"/kv/{ns}/key1/set/value1")
    client.get(f"/kv/{ns}/key2/set/value2")

    listing = client.get(f"/kv/{ns}")
    assert listing.status_code == 200
    # one line per key, format: /kv/<ns>/<key>
    keys = [line for line in listing.text.strip().splitlines() if line]
    assert f"/kv/{ns}/key1" in keys
    assert f"/kv/{ns}/key2" in keys


def test_kv_key_read_returns_untrusted_wrapper(client):
    ns = "zz-kv-http"
    key = "test-read"
    value = "hello from HTTP direct lane"

    client.get(f"/kv/{ns}/{key}/set/{value}")

    r = client.get(f"/kv/{ns}/{key}")
    assert r.status_code == 200
    # raw response starts with the untrusted-content warning
    assert r.text.startswith(UNTRUSTED_PREFIX)
    # actual value is inside
    assert _value(r.text) == value


def test_kv_key_read_nonexistent_returns_404(client):
    r = client.get("/kv/plans/nonexistent-key-xyz-12345")
    assert r.status_code == 404


def test_kv_read_works_after_overwrite(client):
    """A subsequent set overwrites the value, and a subsequent read returns the
    latest one."""
    ns = "zz-kv-http"
    key = "overwrite-test"

    client.get(f"/kv/{ns}/{key}/set/first")
    client.get(f"/kv/{ns}/{key}/set/second")

    r = client.get(f"/kv/{ns}/{key}")
    assert r.status_code == 200
    assert _value(r.text) == "second"


def test_kv_conditional_read_returns_value_regardless_of_if_param(client):
    """?if= on /kv/<ns>/<key> is a no-op for reads — it is only enforced on writes
    (POST /say?if=... and similar). The read lane always returns the current value
    if the key exists, regardless of ?if=.
    This test documents the actual behavior so future refactors don't quietly
    change it without noticing."""
    ns = "zz-kv-http"
    key = "cond-read-doc"
    value = "current-value"

    client.get(f"/kv/{ns}/{key}/set/{value}")

    # ?if= with a non-matching value still returns 200 with the current value
    r = client.get(f"/kv/{ns}/{key}?if=does-not-match")
    assert r.status_code == 200
    assert _value(r.text) == value


def test_kv_if_absent_parameter_on_existing_key(client):
    """?if_absent=1 on a GET is a no-op read — it does not create, it just returns
    the current value of an existing key."""
    ns = "zz-kv-http"
    key = "if-absent-exists"

    client.get(f"/kv/{ns}/{key}/set/original")

    r = client.get(f"/kv/{ns}/{key}?if_absent=1")
    assert r.status_code == 200
    assert _value(r.text) == "original"


def test_kv_read_rejects_uppercase_namespace(client):
    r = client.get("/kv/UPPER/key")
    assert r.status_code == 400


def test_kv_read_rejects_uppercase_key(client):
    r = client.get("/kv/zz-kv-http/UPPER")
    assert r.status_code == 400
