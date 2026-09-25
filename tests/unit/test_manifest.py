"""Tests for the pure helpers in src/manifest.py.

manifest.py ships the OpenAPI + agent-manifest documents, and until now had no unit tests.
These pin the pure, side-effect-free helpers so a future refactor of the document builders
cannot silently change the URL/base behavior or the number formatting the documents publish.
(The documents are built from enforced constants on purpose — a published limit that disagrees
with the enforced one is worse than none — so the formatting they rely on must stay stable.)
"""

from __future__ import annotations

import manifest


def test_public_base_configured_wins():
    assert manifest.public_base("https", "example.com", "https://chat.example.org") == "https://chat.example.org"


def test_public_base_strips_trailing_slash():
    assert manifest.public_base("https", "example.com", "https://chat.example.org/") == "https://chat.example.org"


def test_public_base_uses_valid_host():
    assert manifest.public_base("https", "chat.example.com", "") == "https://chat.example.com"
    assert manifest.public_base("http", "chat.example.com:8080", "") == "http://chat.example.com:8080"


def test_public_base_rejects_non_hostname():
    # attacker-controlled Host header that is not a plausible authority -> fall back to ""
    assert manifest.public_base("https", "evil.com/foo", "") == ""
    assert manifest.public_base("https", "@evil.com", "") == ""
    assert manifest.public_base("ftp", "example.com", "") == ""  # scheme not in (http, https)
    assert manifest.public_base("https", "", "") == ""


def test_url_builds_absolute_or_relative():
    assert manifest._url("https://chat.example.com", "/r/room") == "https://chat.example.com/r/room"
    assert manifest._url("", "/r/room") == "/r/room"  # relative fallback when no trustworthy base


def test_published_number_preserves_integers():
    assert manifest._published_number(10.0) == 10
    assert manifest._published_number(10) == 10
    assert manifest._published_number(0.5) == 0.5  # fractional stays float


def test_host_re_accepts_and_rejects():
    assert manifest._HOST_RE.match("chat.example.com")
    assert manifest._HOST_RE.match("localhost:8080")
    assert not manifest._HOST_RE.match("")
    assert not manifest._HOST_RE.match("a" * 300)  # too long


def test_schema_constants_track_store_limits():
    # The manifest must publish exactly the store-enforced limits, or a machine reader
    # believes a different cap than the server enforces.
    import store

    assert manifest._TEXT_SCHEMA["maxLength"] == store.MAX_TEXT_CHARS
    assert manifest._VALUE_SCHEMA["maxLength"] == store.MAX_VALUE_CHARS
