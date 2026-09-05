#!/usr/bin/env python3
"""operator_digest.py -- read /stats and flag the failure patterns README.md documents.

Run it as the scheduled job /stats' own docstring expects ("the one caller is a scheduled
job"): a cron entry or a monitoring check, pointed at your own deployment with your own
CHAT_STATS_TOKEN. Nothing here is a new server feature -- it is a client for the endpoint
that already exists, checking the numbers against the thresholds README.md already states
in prose (room/note/disk capacity nearing exhaustion, and the client_identity pattern that
means CHAT_CLIENT_IP_HEADER is misconfigured).

Usage:
    CHAT_STATS_TOKEN=... python3 scripts/operator_digest.py --url https://your-deployment
    python3 scripts/operator_digest.py --url https://your-deployment --token ... --warn-pct 80

Exit code is 0 when every check is clear, 1 when at least one WARN fired, and 2 on a
request/parse failure -- so it composes directly into a cron job or an alerting pipeline
without a wrapper script.

No dependency beyond the standard library: this is meant to run anywhere the deployment
itself runs, without adding an install step to a health check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def fetch_stats(url: str, token: str, timeout: float) -> dict:
    """GET {url}/stats with the token header. Raises on any non-200 or malformed body --
    a monitoring check that silently treats a broken fetch as "all clear" is worse than one
    that fails loudly, so this never swallows an error into a default digest.
    """
    req = urllib.request.Request(
        url.rstrip("/") + "/stats",
        headers={"x-stats-token": token, "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"/stats returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def validate_stats(stats: object) -> dict:
    """Fail loudly rather than defaulting into a false all-clear.

    Every key checked here is present in every real /stats response -- service_stats()
    always includes rooms, notes, bytes, requests and client_identity, whether or not the
    numbers inside them are interesting. A response missing one, or where one is present
    but not the object it should be, is schema drift or a truncated body, not an
    unconfigured knob -- and build_digest()'s .get(key, 0) defaults would otherwise turn
    that into a silent, wrong "nothing to report" instead of the loud failure this script's
    own docstring promises. Raises ValueError, caught by main() alongside the fetch errors
    it already treats as exit 2.
    """
    if not isinstance(stats, dict):
        raise ValueError(f"/stats body is not a JSON object (got {type(stats).__name__})")
    required = ("rooms", "notes", "bytes", "requests", "client_identity")
    missing = [k for k in required if not isinstance(stats.get(k), dict)]
    if missing:
        raise ValueError(
            f"/stats body is missing or has a malformed value for: {', '.join(missing)}"
        )
    return stats


def check_capacity(name: str, used: int, cap: int, warn_pct: float) -> str | None:
    """One line, or None if this cap isn't close. Division-by-zero-safe: a cap of 0 (a
    deployment that has disabled something) is never a false WARN.
    """
    if cap <= 0:
        return None
    pct = 100.0 * used / cap
    if pct >= warn_pct:
        return f"WARN  {name}: {used}/{cap} ({pct:.1f}%) -- past the {warn_pct:g}% mark"
    return None


def check_client_identity(stats: dict) -> str | None:
    """The exact pattern README.md's CHAT_CLIENT_IP_HEADER section names: proxied requests
    are arriving with a CDN's own client-IP header while the deployment is configured to
    ignore it, and distinct_identities is stuck near 1 -- meaning every caller shares one
    rate-limit bucket, including the per-day room budget, which then bounds the whole
    internet at once rather than per caller.
    """
    ci = stats.get("client_identity", {})
    ignored = ci.get("proxied_requests_ignored", 0)
    distinct = ci.get("distinct_identities", 0)
    header = ci.get("client_ip_header")
    if header is None and ignored > 0 and distinct <= 3:
        return (
            f"WARN  client_identity: {ignored} proxied requests ignored, only {distinct} "
            "distinct identities seen, and CHAT_CLIENT_IP_HEADER is unset -- every caller "
            "may be sharing one rate-limit bucket. If a CDN sits in front of this "
            "deployment, point CHAT_CLIENT_IP_HEADER at the header it overwrites (e.g. "
            "cf-connecting-ip for Cloudflare) once the origin is unreachable except "
            "through it."
        )
    return None


def build_digest(stats: dict, warn_pct: float) -> tuple[list[str], list[str]]:
    """Returns (info_lines, warn_lines). Every check here reads a field service_stats()
    documents and compares it against a threshold README.md states in prose -- nothing is
    inferred beyond what the deployment already publishes about itself.
    """
    rooms = stats.get("rooms", {})
    notes = stats.get("notes", {})
    byts = stats.get("bytes", {})
    reqs = stats.get("requests", {})

    info = [
        f"rooms:    {rooms.get('total', '?')}/{rooms.get('capacity', '?')} "
        f"(listed {rooms.get('listed', '?')}, unlisted {rooms.get('unlisted', '?')}, "
        f"mailbox {rooms.get('mailbox', '?')}, ownable {rooms.get('ownable', '?')}, "
        f"ephemeral {rooms.get('ephemeral', '?')})",
        f"notes:    {notes.get('total', '?')}/{notes.get('capacity', '?')} "
        f"(per-namespace cap {notes.get('capacity_per_namespace', '?')})",
        f"bytes:    rooms {byts.get('rooms', '?')}/{byts.get('rooms_capacity', '?')}, "
        f"notes {byts.get('notes', '?')}",
        f"requests: {reqs.get('scope', '?')}, uptime {reqs.get('uptime_seconds', '?')}s, "
        f"workers {reqs.get('workers', '?')} -- multiply request counts by this figure "
        "for a service-wide estimate (see README's 'Running more than one worker')",
    ]

    warn = []
    for check in (
        check_capacity("rooms", rooms.get("total", 0), rooms.get("capacity", 0), warn_pct),
        check_capacity("notes", notes.get("total", 0), notes.get("capacity", 0), warn_pct),
        check_capacity("room bytes", byts.get("rooms", 0), byts.get("rooms_capacity", 0), warn_pct),
        check_client_identity(stats),
    ):
        if check:
            warn.append(check)
    return info, warn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--url", required=True, help="deployment base URL, e.g. https://your-host")
    parser.add_argument(
        "--token",
        default=os.environ.get("CHAT_STATS_TOKEN"),
        help="stats token; defaults to $CHAT_STATS_TOKEN",
    )
    parser.add_argument(
        "--warn-pct",
        type=float,
        default=90.0,
        help="capacity percentage that triggers a WARN (default: 90)",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="request timeout in seconds")
    parser.add_argument(
        "--quiet", action="store_true", help="print only WARN lines, not the full digest"
    )
    args = parser.parse_args()

    if not args.token:
        print("error: no stats token given (--token or $CHAT_STATS_TOKEN)", file=sys.stderr)
        return 2

    try:
        stats = validate_stats(fetch_stats(args.url, args.token, args.timeout))
    except (
        urllib.error.URLError,
        TimeoutError,
        RuntimeError,
        json.JSONDecodeError,
        ValueError,
    ) as e:
        print(f"error: could not read /stats: {e}", file=sys.stderr)
        return 2

    info, warn = build_digest(stats, args.warn_pct)

    if not args.quiet:
        for line in info:
            print(line)
        if warn:
            print()

    for line in warn:
        print(line)

    return 1 if warn else 0


if __name__ == "__main__":
    sys.exit(main())
