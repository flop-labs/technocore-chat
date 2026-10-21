"""Configuration rules, which is where this package's security properties live."""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from technocore_exporter.fetch import DEFAULT_TIMEOUT
from technocore_exporter.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_URL,
    SCRAPE_TIMEOUT_CEILING,
    build,
    build_registry,
    describe,
    main,
    settings,
)

TOKEN = {"TECHNOCORE_STATS_TOKEN": "s3cret-token"}


def test_a_missing_token_stops_the_process_with_the_reason():
    """Not a warning and not a 404 loop: without the token there is nothing to export, and
    the failure mode at the origin (404, not 401) is the one an operator misreads."""
    with pytest.raises(SystemExit, match="CHAT_STATS_TOKEN"):
        settings(env={})


def test_an_empty_token_counts_as_missing():
    with pytest.raises(SystemExit, match="TECHNOCORE_STATS_TOKEN is not set"):
        settings(env={"TECHNOCORE_STATS_TOKEN": ""})


def test_the_defaults_are_loopback_and_the_local_origin():
    """Loopback by default because /metrics is not gated at all."""
    config = settings(env=TOKEN)
    assert config.host == DEFAULT_HOST == "127.0.0.1"
    assert config.port == DEFAULT_PORT == 9464
    assert config.url == DEFAULT_URL
    # Below Prometheus's common 10s scrape_timeout, so a slow origin is reported as a
    # failed scrape rather than swallowed by the scrape timing out with no samples.
    assert config.timeout == DEFAULT_TIMEOUT < 10.0


def test_every_setting_is_overridable():
    config = settings(
        env={
            **TOKEN,
            "TECHNOCORE_STATS_URL": "https://chat.example/stats",
            "TECHNOCORE_EXPORTER_HOST": "0.0.0.0",
            "TECHNOCORE_EXPORTER_PORT": "9999",
            "TECHNOCORE_STATS_TIMEOUT": "2.5",
        }
    )
    assert config.url == "https://chat.example/stats"
    assert (config.host, config.port, config.timeout) == ("0.0.0.0", 9999, 2.5)


def test_the_token_never_reaches_the_startup_log():
    """Asserted rather than left to a reviewer noticing a future format string change."""
    config = settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "https://chat.example/stats"})
    line = describe(config)
    assert "s3cret-token" not in line
    assert "chat.example" in line and "9464" in line


def test_the_collector_is_wired_with_the_configured_values():
    registry = CollectorRegistry()
    config = settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "https://chat.example/stats"})
    collector = build(config, registry=registry)
    assert collector._url == "https://chat.example/stats"
    assert collector._token == "s3cret-token"
    assert collector._timeout == DEFAULT_TIMEOUT


# ------------------------------------------------- boot validation, reported by @Minh3132


@pytest.mark.parametrize(
    "raw", ["-1", "-0.5", "nan", "NaN", "inf", "-inf", "Infinity", "0", "abc", ""]
)
def test_an_unusable_timeout_is_refused_at_boot(raw):
    """The failure mode this closes is worse than a wrong number.

    A negative or NaN timeout raises ValueError from socket.settimeout and inf raises
    OverflowError — on every scrape, not at boot. Since collect() deliberately converts any
    unexpected exception into scrape_success 0, an unvalidated typo here produces a process
    that starts, stays up, answers /metrics forever and can never once succeed. Refusing at
    boot is the difference between a visible misconfiguration and an exporter that looks
    alive and is not.
    """
    with pytest.raises(SystemExit, match="TECHNOCORE_STATS_TIMEOUT"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_TIMEOUT": raw})


def test_a_timeout_at_or_above_the_scrape_ceiling_is_refused():
    """The headroom in the README is part of the contract, so it is enforced rather than
    described: at or above Prometheus's own default scrape_timeout the source can no longer
    fail first, and the failure lands as a scrape timeout with no samples."""
    with pytest.raises(SystemExit, match="must be below"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_TIMEOUT": str(SCRAPE_TIMEOUT_CEILING)})
    assert settings(env={**TOKEN, "TECHNOCORE_STATS_TIMEOUT": "9.9"}).timeout == 9.9


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999", "abc", "8.5", ""])
def test_an_unusable_port_is_refused_at_boot(raw):
    with pytest.raises(SystemExit, match="TECHNOCORE_EXPORTER_PORT"):
        settings(env={**TOKEN, "TECHNOCORE_EXPORTER_PORT": raw})


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a url",
        "ftp://host/stats",
        "file:///etc/passwd",
        "/stats",
        "http:///stats",
        # The port, which the scheme check does not see. `http://127.0.0.1:notaport/stats`
        # parsed clean, booted clean, and then failed every scrape with `protocol error:
        # InvalidURL` — accepted at boot and permanently unable to work, which is the exact
        # outcome this validator exists to prevent.
        "http://127.0.0.1:notaport/stats",
        "http://127.0.0.1:99999/stats",
        "http://127.0.0.1:0/stats",
        # `urlparse` raises on this one before any check runs, so the process died with a
        # traceback instead of the refusal every other setting here gets.
        "http://[::1/stats",
    ],
)
def test_an_unusable_stats_url_is_refused_at_boot(raw):
    """Same class as the timeout: `urllib.request.Request` raises for an unknown scheme at
    *request* time, which the broad catch in collect() would render as a permanently
    failing scrape rather than as the configuration error it is.

    Every case here must reach `SystemExit` with the variable named. A traceback is not a
    refusal: it is the same "unusable configuration" outcome reported in a way that does
    not tell the operator which knob to fix.
    """
    with pytest.raises(SystemExit, match="TECHNOCORE_STATS_URL"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_URL": raw})


@pytest.mark.parametrize("raw", [" ", "\t", "\n", "   "])
def test_a_whitespace_only_token_counts_as_missing(raw):
    """`TECHNOCORE_STATS_TOKEN=" "` is truthy, so it booted and 404ed forever.

    Same shape as an unusable URL or timeout: accepted at boot, permanently unable to work.
    A stray space in a unit file or a `.env` line is how it happens.
    """
    with pytest.raises(SystemExit, match="TECHNOCORE_STATS_TOKEN is only whitespace"):
        settings(env={"TECHNOCORE_STATS_TOKEN": raw})


def test_a_token_whose_whitespace_is_real_is_sent_unchanged():
    """The other direction, and the reason the value is tested stripped but used raw.

    Core reads CHAT_STATS_TOKEN without stripping and compares with `compare_digest`, so a
    token with surrounding whitespace is one this must send exactly as configured.
    Trimming it here would turn a working deployment into a silent 404.
    """
    config = settings(env={"TECHNOCORE_STATS_TOKEN": " padded "})
    assert config.token == " padded "


def test_the_usable_configuration_is_still_accepted():
    """The other direction, so the validators cannot pass by refusing everything."""
    config = settings(
        env={
            **TOKEN,
            "TECHNOCORE_STATS_URL": "https://chat.example/stats",
            "TECHNOCORE_EXPORTER_PORT": "9999",
            "TECHNOCORE_STATS_TIMEOUT": "2.5",
        }
    )
    assert (config.url, config.port, config.timeout) == ("https://chat.example/stats", 9999, 2.5)


@pytest.mark.parametrize(
    "raw",
    [
        "http://user:s3cret@host/stats",
        "http://user@host/stats",
        "https://admin:hunter2@chat.example/stats",
    ],
)
def test_a_url_embedding_credentials_is_refused(raw):
    """`describe()` writes this URL to the startup log, so URL credentials would put a
    second secret in the one place the stats token is deliberately kept out of. /stats
    authenticates with X-Stats-Token and nothing else, so these can only be a mistake."""
    with pytest.raises(SystemExit, match="must not embed credentials"):
        settings(env={**TOKEN, "TECHNOCORE_STATS_URL": raw})


def test_the_refusal_does_not_echo_the_credential():
    """A refusal that quotes the value would write the secret to stderr instead."""
    try:
        settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "http://user:s3cret@host/stats"})
    except SystemExit as exc:
        assert "s3cret" not in str(exc)
    else:
        raise AssertionError("expected a refusal")


# ------------------------------------ what the ungated page exposes, reported by @Minh3132


def _served_names(body: str) -> set:
    return {family.name for family in text_string_to_metric_families(body)}


def test_the_served_registry_carries_only_our_families(monkeypatch, stats):
    """The boundary, asserted on a rendered page rather than on the registration call.

    `build` used to default to the client library's global `REGISTRY`, which arrives
    pre-populated with the process, platform and GC collectors — so the ungated `/metrics`
    page published `python_info`, `process_resident_memory_bytes` and friends alongside the
    digest. Rendering the page is what makes that visible; asserting on what was registered
    is not, because those families are registered by an import, not by this package.
    """
    monkeypatch.setattr("technocore_exporter.collector.fetch_stats", lambda *a, **k: stats)
    config = settings(env={**TOKEN, "TECHNOCORE_STATS_URL": "https://chat.example/stats"})

    body = generate_latest(build_registry(config)).decode()

    names = _served_names(body)
    assert names, "the served page is empty"
    assert all(name.startswith("technocore_") for name in names), sorted(names)


def test_main_serves_the_registry_it_built(monkeypatch, stats):
    """The production wiring, not a stand-in for it.

    A test that only renders `build_registry(config)` still passes if `main()` goes back to
    calling `start_http_server` without a registry, because the library then serves its own
    default and never consults ours. So this drives `main()` itself and asserts on the
    registry the serve call actually received.
    """
    captured = {}

    def fake_start(port, addr, registry):
        captured.update(port=port, addr=addr, registry=registry)

    def stop(*_args):
        raise SystemExit

    monkeypatch.setattr("technocore_exporter.server.start_http_server", fake_start)
    monkeypatch.setattr("technocore_exporter.server.time.sleep", stop)
    monkeypatch.setattr("technocore_exporter.collector.fetch_stats", lambda *a, **k: stats)
    monkeypatch.setenv("TECHNOCORE_STATS_TOKEN", "s3cret-token")
    monkeypatch.setenv("TECHNOCORE_STATS_URL", "https://chat.example/stats")
    monkeypatch.setenv("TECHNOCORE_EXPORTER_PORT", "9999")
    # main() reads the real environment. Cleared rather than trusted, so a developer who
    # exports one of these for a live exporter does not get a failure from this file.
    monkeypatch.delenv("TECHNOCORE_STATS_TIMEOUT", raising=False)
    monkeypatch.delenv("TECHNOCORE_EXPORTER_HOST", raising=False)

    with pytest.raises(SystemExit):
        main()

    assert (captured["port"], captured["addr"]) == (9999, DEFAULT_HOST)
    body = generate_latest(captured["registry"]).decode()
    assert all(name.startswith("technocore_") for name in _served_names(body))
    for upstream in ("python_info", "process_resident_memory_bytes", "python_gc_objects"):
        assert upstream not in body, f"{upstream} reached the ungated page"


def test_importing_the_module_entrypoint_does_not_start_the_server(monkeypatch):
    """`__main__.py` ran `main()` at import, with no `if __name__` guard.

    Anything that imports the package's submodules — a docs tool, `pkgutil.walk_packages`,
    a packaging or coverage pass — therefore bound a port and never returned. The guard is
    the stdlib convention; `mcp/` has no `__main__.py` to copy, so there was no in-repo
    precedent to follow either.

    Asserted through a real import rather than by reading the source for the guard, because
    a source scan passes for a file that has the words in a comment.
    """
    import importlib

    called = []
    monkeypatch.setattr(
        "technocore_exporter.server.main", lambda: called.append(True), raising=True
    )
    # Reloaded rather than merely imported: the module may already be in sys.modules from
    # an earlier test, and a cached import executes nothing at all — which would make this
    # pass whether or not the guard is there.
    importlib.reload(importlib.import_module("technocore_exporter.__main__"))
    assert called == [], "importing __main__ ran main()"
