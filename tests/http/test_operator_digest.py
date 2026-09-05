"""scripts/operator_digest.py's CLI is a documented contract -- test it like one.

Every check it makes is a specific field comparison against a threshold README.md states
in prose. These tests pin the exact WARN/exit-code behaviour for each documented pattern
(capacity nearing exhaustion, the client_identity misconfiguration shape), plus the
error paths (bad token, unreachable host), against a real subprocess invocation -- not an
imported function -- so a change to the CLI's own argument handling is covered too.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIGEST = ROOT / "scripts" / "operator_digest.py"
TOKEN = "test-token-value"

BASE_STATS = {
    "rooms": {
        "total": 50,
        "listed": 40,
        "unlisted": 10,
        "open": 30,
        "mailbox": 5,
        "ownable": 4,
        "ephemeral": 3,
        "capacity": 5120,
    },
    "notes": {"total": 10, "bytes": 5000, "capacity": 163840, "capacity_per_namespace": 5120},
    "bytes": {"rooms": 40000, "rooms_capacity": 5368709120, "notes": 5000},
    "counters": {},
    "engagement": {},
    "history": [],
    "requests": {"uptime_seconds": 60, "scope": "per_worker", "workers": 1},
    "client_identity": {
        "client_ip_header": "cf-connecting-ip",
        "distinct_identities": 40,
        "proxied_requests_ignored": 0,
    },
    "capacity_limits": {},
}


def _serve(stats: dict, port: int) -> HTTPServer:
    """A throwaway HTTP server on localhost answering exactly one shape: /stats gated on
    TOKEN, everything else 404 -- close enough to the real endpoint's own gate (a wrong or
    missing token is indistinguishable from an unrouted path) to exercise the CLI's error
    path honestly.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/stats" and self.headers.get("x-stats-token") == TOKEN:
                body = json.dumps(stats).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # quiet: pytest captures enough
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(url: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DIGEST), "--url", url, "--token", TOKEN, *extra_args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_a_clear_deployment_prints_the_digest_and_exits_zero() -> None:
    server = _serve(BASE_STATS, 18211)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 0
    assert "rooms:    50/5120" in result.stdout
    assert "WARN" not in result.stdout


def test_room_capacity_past_the_threshold_warns_and_exits_one() -> None:
    stats = json.loads(json.dumps(BASE_STATS))  # cheap deep copy
    stats["rooms"]["total"] = 4900  # 95.7% of 5120
    server = _serve(stats, 18212)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "WARN  rooms: 4900/5120" in result.stdout


def test_a_custom_warn_pct_moves_the_threshold() -> None:
    stats = json.loads(json.dumps(BASE_STATS))
    stats["rooms"]["total"] = 3000  # 58.6% -- clear at 90%, WARN at 50%
    server = _serve(stats, 18213)
    try:
        clear = run(f"http://127.0.0.1:{server.server_port}")
        warns = run(f"http://127.0.0.1:{server.server_port}", "--warn-pct", "50")
    finally:
        server.shutdown()
    assert clear.returncode == 0
    assert warns.returncode == 1 and "WARN  rooms:" in warns.stdout


def test_the_misconfigured_proxy_header_pattern_is_named_exactly() -> None:
    """The pattern README.md's CHAT_CLIENT_IP_HEADER section describes: proxied requests
    ignored, distinct_identities stuck near 1, header unset."""
    stats = json.loads(json.dumps(BASE_STATS))
    stats["client_identity"] = {
        "client_ip_header": None,
        "distinct_identities": 1,
        "proxied_requests_ignored": 500,
    }
    server = _serve(stats, 18214)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "CHAT_CLIENT_IP_HEADER" in result.stdout
    assert "cf-connecting-ip" in result.stdout  # the worked example, not just the knob name


def test_a_healthy_client_identity_never_warns_even_with_some_ignored_traffic() -> None:
    """The check is about the *pattern* (few identities plus ignored traffic), not the
    presence of any ignored request at all -- a deployment can have both a correctly
    configured header and a handful of direct-to-origin probes."""
    stats = json.loads(json.dumps(BASE_STATS))
    stats["client_identity"] = {
        "client_ip_header": "cf-connecting-ip",
        "distinct_identities": 400,
        "proxied_requests_ignored": 3,
    }
    server = _serve(stats, 18215)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 0
    assert "CHAT_CLIENT_IP_HEADER" not in result.stdout


def test_quiet_suppresses_the_digest_but_keeps_the_warnings() -> None:
    stats = json.loads(json.dumps(BASE_STATS))
    stats["rooms"]["total"] = 4900
    server = _serve(stats, 18216)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}", "--quiet")
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "rooms:    4900/5120 (listed" not in result.stdout  # the info line
    assert "WARN  rooms:" in result.stdout  # the warning survives


def test_an_empty_stats_body_is_a_clean_error_not_a_false_all_clear() -> None:
    """@yukkie3276's review of #672: build_digest()'s .get(key, 0) defaults let a
    syntactically valid but empty body sail through as a clean digest -- exit 0, no WARN --
    which is exactly the false all-clear this script's own docstring says a monitoring
    check must never produce. {} is valid JSON and a valid dict; it is not a valid /stats
    body, and validate_stats() is what tells the two apart.
    """
    server = _serve({}, 18218)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 2
    assert "error" in result.stderr.lower()


def test_one_missing_top_level_field_is_a_clean_error_not_a_partial_digest() -> None:
    """The narrower case the review named: a body with everything except one required
    field (schema drift, or a body truncated mid-transfer that still happens to end on a
    valid JSON boundary) must fail the same way a fully empty one does, not silently check
    only what happened to survive.
    """
    stats = json.loads(json.dumps(BASE_STATS))
    del stats["client_identity"]
    server = _serve(stats, 18219)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 2
    assert "client_identity" in result.stderr


def test_a_required_field_present_but_wrong_type_is_also_a_clean_error() -> None:
    """Present is not the same as well-formed: a caller feeding this a hand-edited or
    mocked body could easily get the shape wrong (a string instead of an object) while
    still tripping only a presence check, not a type one."""
    stats = json.loads(json.dumps(BASE_STATS))
    stats["rooms"] = "not an object"
    server = _serve(stats, 18220)
    try:
        result = run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
    assert result.returncode == 2
    assert "rooms" in result.stderr


def test_a_wrong_token_is_a_clean_error_not_a_false_clear() -> None:
    server = _serve(BASE_STATS, 18217)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(DIGEST),
                "--url",
                f"http://127.0.0.1:{server.server_port}",
                "--token",
                "wrong",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.shutdown()
    assert result.returncode == 2
    assert "error" in result.stderr.lower()


def test_an_unreachable_host_is_a_clean_error() -> None:
    result = subprocess.run(
        [sys.executable, str(DIGEST), "--url", "http://127.0.0.1:1", "--token", TOKEN],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "error" in result.stderr.lower()


def test_no_token_anywhere_is_a_clean_error_before_any_request() -> None:
    result = subprocess.run(
        [sys.executable, str(DIGEST), "--url", "http://127.0.0.1:1"],
        capture_output=True,
        text=True,
        timeout=15,
        env={},  # no CHAT_STATS_TOKEN in the environment either
    )
    assert result.returncode == 2
    assert "token" in result.stderr.lower()
