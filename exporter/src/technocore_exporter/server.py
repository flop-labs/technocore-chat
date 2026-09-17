"""Serve /metrics.

`start_http_server` from the client library rather than a hand-rolled handler: it already
answers with the right Content-Type, handles the OpenMetrics negotiation Prometheus does,
and is the surface the library's own tests cover.

Split into `settings()` / `build()` / `main()` so the configuration rules — which are where
the security properties live — are testable without binding a socket or entering the
serve loop.
"""

from __future__ import annotations

import logging
import math
import os
import time
import urllib.parse
from typing import NamedTuple

from prometheus_client import CollectorRegistry, start_http_server

from .collector import TechnocoreCollector
from .fetch import DEFAULT_TIMEOUT

log = logging.getLogger("technocore_exporter")

DEFAULT_URL = "http://127.0.0.1:8080/stats"
DEFAULT_PORT = 9464
DEFAULT_HOST = "127.0.0.1"


class Settings(NamedTuple):
    url: str
    token: str
    host: str
    port: int
    timeout: float


# The scrape timeout this source timeout must stay under. Prometheus's own default, and
# the figure the README's headroom argument is written against: a source timeout at or
# above it cannot fail before the scrape does, so the failure lands as a scrape timeout
# with no samples instead of as scrape_success 0 with telemetry.
#
# This bounds the origin read, not one socket operation, and only because two other things
# now hold: `_gather` spends one deadline on the lock wait and the fetch together, and
# `_read_within` enforces it across a trickling body. Before those, this number bounded
# nothing — against a trickling origin on the 5s default, one fetch took 20.01s and the
# `GET /metrics` around it answered after 20.18s, reporting success — and the arithmetic
# here was describing a guarantee the code did not make.
SCRAPE_TIMEOUT_CEILING = 10.0


def _refuse(message: str):
    return SystemExit(f"technocore-exporter: {message}")


def _seconds(raw: str) -> float:
    """A timeout from the environment, or refuse to start.

    Same reasoning core states for `config._finite_env`: `int()` raises on junk and takes
    the process down, which is the loudest way to report bad configuration, while
    `float()` accepts `inf` and `nan` happily. Here the consequence is specific and worse
    than a wrong number. A negative or NaN value raises ValueError from `socket.settimeout`
    and `inf` raises OverflowError — on *every* scrape, not at boot — and `collect()` now
    deliberately converts any unexpected exception into `scrape_success 0`. So a typo in
    this knob would produce a process that starts, stays up, answers /metrics forever and
    can never once succeed. Refusing at boot is the difference between a visible
    misconfiguration and an exporter that looks alive and is not.
    """
    try:
        value = float(raw)
    except ValueError:
        raise _refuse(f"TECHNOCORE_STATS_TIMEOUT must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise _refuse(f"TECHNOCORE_STATS_TIMEOUT must be finite, got {raw!r}")
    if value <= 0:
        # 0 is refused too: socket.settimeout accepts it, but it means non-blocking, so
        # every scrape fails instantly. Accepted-but-always-failing is the case this
        # function exists to prevent.
        raise _refuse(f"TECHNOCORE_STATS_TIMEOUT must be greater than 0, got {raw!r}")
    if value >= SCRAPE_TIMEOUT_CEILING:
        raise _refuse(
            f"TECHNOCORE_STATS_TIMEOUT must be below {SCRAPE_TIMEOUT_CEILING}s so a slow "
            f"origin reports a failed scrape rather than timing out the scrape, got {raw!r}"
        )
    return value


def _port(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise _refuse(f"TECHNOCORE_EXPORTER_PORT must be an integer, got {raw!r}") from None
    if not 1 <= value <= 65535:
        raise _refuse(f"TECHNOCORE_EXPORTER_PORT must be 1-65535, got {raw!r}")
    return value


def _url(raw: str) -> str:
    """Refuse a URL the fetch could only fail on, for the same reason as the timeout.

    `urllib.request.Request` raises ValueError for an unknown scheme at *request* time,
    which the broad catch in collect() would render as a permanently failing scrape.

    Both the parse and the port are inside the guard, and each closed a hole of exactly the
    kind this function exists to close. `urlparse` itself raises on a malformed IPv6 literal
    (`http://[::1/stats`), so this took the process down with a traceback rather than the
    stated refusal every other setting here gets. And a non-numeric or out-of-range port —
    `http://127.0.0.1:notaport/stats` — parsed fine, booted fine, and then failed *every*
    scrape with `protocol error: InvalidURL`: accepted at boot and permanently unable to
    work, which is the one outcome validating configuration here is for.
    """
    try:
        parsed = urllib.parse.urlparse(raw)
        port = parsed.port
    except ValueError:
        raise _refuse(f"TECHNOCORE_STATS_URL is not a usable URL, got {raw!r}") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _refuse(f"TECHNOCORE_STATS_URL must be an http(s) URL, got {raw!r}")
    if port is not None and not 1 <= port <= 65535:
        raise _refuse(f"TECHNOCORE_STATS_URL port must be 1-65535, got {raw!r}")
    if parsed.username or parsed.password:
        # Refused rather than stripped, and the value is not echoed. `describe()` puts this
        # URL in the startup log, so `http://user:secret@host/stats` would write a second
        # credential to the very place the stats token is kept out of. /stats authenticates
        # with X-Stats-Token and nothing else, so URL credentials could only ever be a
        # mistake — and one worth naming rather than silently discarding.
        raise _refuse("TECHNOCORE_STATS_URL must not embed credentials; use the token")
    return raw


def settings(env: dict[str, str] | None = None) -> Settings:
    """Read configuration, refusing anything that could only fail later.

    Every value here is checked at boot rather than at first use. The token comes from the
    environment only: there is no `--token` flag on purpose, because an argv token is
    readable from `ps` by every other user on the host, and this token is the whole gate on
    the digest.

    The host defaults to loopback for the other half of that: the digest is token-gated at
    the origin, but /metrics is not gated at all and carries the same numbers.
    """
    source = os.environ if env is None else env
    # Tested stripped, used raw. `TECHNOCORE_STATS_TOKEN=" "` is what a stray space in a
    # unit file or a `.env` line looks like; it is truthy, so it booted, sent a space and
    # took 404 from /stats forever — accepted at boot and permanently unable to work, the
    # same shape as an unusable URL or timeout.
    #
    # The value itself is *not* stripped, deliberately. `config.STATS_TOKEN` in core reads
    # CHAT_STATS_TOKEN without stripping and compares with `secrets.compare_digest`, so a
    # token whose surrounding whitespace is real is a token this must send unchanged.
    # Trimming here would turn one operator's working configuration into a silent 404.
    token = source.get("TECHNOCORE_STATS_TOKEN", "")
    if not token.strip():
        # Two messages, because they send the operator to different places. "Is not set"
        # is wrong for `TOKEN=" "` — the variable is set, and being told it is not sends
        # someone to check the thing they can already see is there.
        what = "is not set" if not token else "is only whitespace"
        raise _refuse(
            f"TECHNOCORE_STATS_TOKEN {what}. It must match the service's "
            "CHAT_STATS_TOKEN; without it /stats answers 404."
        )
    return Settings(
        url=_url(source.get("TECHNOCORE_STATS_URL", DEFAULT_URL)),
        token=token,
        host=source.get("TECHNOCORE_EXPORTER_HOST", DEFAULT_HOST),
        port=_port(source.get("TECHNOCORE_EXPORTER_PORT", str(DEFAULT_PORT))),
        timeout=_seconds(source.get("TECHNOCORE_STATS_TIMEOUT", str(DEFAULT_TIMEOUT))),
    )


def build(config: Settings, registry: CollectorRegistry) -> TechnocoreCollector:
    """Register the collector. Separate from main() so a test can assert what was wired.

    `registry` has no default on purpose. It used to default to the client library's global
    `REGISTRY`, which is not empty: importing `prometheus_client` pre-registers the process,
    platform and GC collectors, so whatever this exporter served carried `python_info`,
    `process_resident_memory_bytes`, `process_cpu_seconds`, `process_open_fds` and the GC
    families beside ours (reported by @Minh3132). A default that quietly widens an
    unauthenticated endpoint is the wrong default, so there is none.
    """
    collector = TechnocoreCollector(config.url, config.token, config.timeout)
    registry.register(collector)
    return collector


def build_registry(config: Settings) -> CollectorRegistry:
    """The registry `/metrics` serves: this exporter's families and nothing else.

    This is the call `main()` makes, so the boundary is decided in one testable place rather
    than in the serve loop. Two claims depend on it and both are checkable from here: the
    first-wave scope is what `store.service_stats` returns plus exporter self-metrics, and
    the README tells operators that the ungated `/metrics` page carries the same numbers as
    the token-gated digest. A process/runtime surface arriving from the library's global
    default would contradict both, and would change with an upstream release rather than
    with a change here.
    """
    registry = CollectorRegistry()
    build(config, registry)
    return registry


def describe(config: Settings) -> str:
    """The startup log line. Its own function because what it must NOT contain is a rule.

    The URL is logged and the token never is, at any level — so this is asserted rather
    than left to a reviewer noticing a future `%s` gaining an argument.
    """
    return f"serving /metrics on {config.host}:{config.port}, reading {config.url}"


# no cover: the `while True` never completes, so the line after it can never be reached.
# main() itself is exercised by test_main_serves_the_registry_it_built, which stubs the
# serve call and the sleep — the pragma is about the loop, not about the wiring above it.
def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = settings()
    registry = build_registry(config)
    start_http_server(config.port, addr=config.host, registry=registry)
    log.info("%s", describe(config))
    while True:
        time.sleep(3600)
