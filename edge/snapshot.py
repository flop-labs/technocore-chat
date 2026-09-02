#!/usr/bin/env python3
"""Snapshot the document surface into edge/public/ for the fallback Worker.

Run against a live instance immediately before `wrangler deploy`, so the stored copy is the
one that release actually serves:

    uv run edge/snapshot.py --base https://technocore.chat

Why fetched rather than rendered from source: several of these documents are assembled from
the *running* configuration — /llms.txt carries MAX_ROOMS, MAX_NOTES_PER_NS and the duplicate
window, /openapi.json and /.well-known/agent.json carry the version and the whole `limits`
object. Rendering them here would mean a second implementation of that assembly, which is
the drift this repo has already been bitten by once. Asking the service what it serves has
exactly one source of truth.

The content type of each path is recorded beside the bytes. Six of these paths have no file
extension (/humans, /config, /.well-known/api-catalog, ...), and an asset server guessing
from the name would hand a browser text/plain HTML or octet-stream JSON.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import store  # noqa: E402  — for MAX_LIMIT only; see ROOMS_KEY_MATCH below

# Every route in app.py that renders a document. Deliberately explicit: this list and the
# `routes` in wrangler.jsonc describe the same surface and are checked against each other
# by test_edge_snapshot_covers_every_routed_document.
PATHS = (
    "/",
    "/llms.txt",
    "/skill.md",
    "/patterns.md",
    "/interop.md",
    "/auth.md",
    "/humans",
    "/robots.txt",
    "/sitemap.xml",
    "/openapi.json",
    "/config",
    "/.well-known/agent.json",
    "/.well-known/api-catalog",
    "/.well-known/ai-catalog.json",
    "/.well-known/agent-skills/index.json",
    "/.well-known/mcp/server-card.json",
    "/.well-known/security.txt",
)


# Served from the stored copy without asking the origin at all, and therefore restricted to
# documents whose bytes owe nothing to the running configuration.
#
# /robots.txt is NOT here, though it looks like it belongs: manifest.robots_txt embeds an
# absolute Sitemap URL built from CHAT_PUBLIC_URL, so an operator moving the public origin
# is exactly the compose-time change this split exists to survive. It stays origin-first.
#
# Their bytes are taken from THIS CHECKOUT rather than from the fetch below. On every deploy
# after the first, --base is already routed through the deployed Worker, which answers these
# paths from its own stored copy without asking the origin — so a fetch would re-capture the
# previous snapshot and freeze these two documents at their first upload forever. Reading
# the source tree also ties them to the release being deployed, which is the thing a reader
# hitting the static lane should get.
#
# The tradeoff taken: these change on a RELEASE, so a stored copy is stale until deploy.sh
# runs again. That is accepted because they are the documents a reader needs when the origin
# is degraded rather than down — the 2026-09-01 outage spent hours slow-but-alive, where
# origin-first waits out its timeout before falling back — and because a release is a
# controlled moment where re-running deploy.sh is a checklist item, unlike a compose edit.
STATIC_FIRST = {"/skill.md": "SKILL.md", "/patterns.md": "src/patterns.md"}

# Held by the edge for a few seconds and NEVER snapshotted — the third lane, and the
# distinction is the point. A liveness endpoint must not have a stored copy to fall back on,
# because the only thing a stored "ok" can do is answer for a service that is gone. So this
# lane caches and never falls back, and these paths are deliberately absent from PATHS.
#
# 10s, not 1s: Cloudflare caches per PoP, so 20.7 req/s across the PoP network arrives at any
# single one far more slowly than the global rate suggests and a 1s window mostly misses. The
# same fragmentation made a 3s window buy almost nothing for /rooms. 10s cannot reach an
# alerting decision — monitors alert on two or three consecutive failures — and it removes
# ~1.78M origin requests a day (measured 2026-09-02: 2,478 of 2,480 /healthz requests in two
# minutes arrived through the tunnel, 10.4% of all traffic).
EDGE_CACHED = {"/healthz": 10}

# Served from the edge copy ALWAYS, refreshed in the background at most this often. Never
# snapshotted: like /healthz these are live figures, and a stored answer for them would
# outlive the service that produced it.
#
# This lane exists because /rooms cannot be made fast enough to wait for — and the reason is
# not that the walk is expensive. bench/rooms.py measures it at tens of milliseconds on an
# idle store, less than half of which is what `limit` buys; the same walk against concurrent
# writers costs an order of magnitude more at *every* limit, including the limit that reads
# no room tails at all. The cost is queueing, not work, so walking less does not remove it.
#
# Plain caching cannot cover a cost with no upper bound. The origin's window is finite —
# s-maxage plus stale-while-revalidate, both derived from CHAT_EDGE_CACHE_SECONDS — so
# whenever a walk outlasts it the next reader pays the whole cost, and occupies an anyio
# thread while doing so, which is the resource the box actually runs out of.
#
# Serving the stale copy unconditionally and refreshing behind ctx.waitUntil() inverts that:
# no reader ever waits for a walk, and the data is as fresh as the origin can actually
# produce rather than as fresh as we are willing to make people wait. The interval also
# bounds how often the origin is asked, which is the part that frees the thread pool.
EDGE_REVALIDATE = {"/rooms": 60}

# What a /rooms cache key is made of. The handler reads exactly two query parameters and
# clamps one of them, so /rooms?limit=999999999, /rooms?limit=200 and /rooms?limit=200&x=1
# are one reply — and a cache keyed on the raw URL stores them as three, which hands a caller
# a way to force a cold walk on every request by incrementing a digit. app.py fixed exactly
# this for its own cache ("the key space is the reply space"); a lane that caches in front of
# it has to carry the same fix, or it reintroduces the bug one layer out.
#
# `match` names a parameter that matters only when it equals one value: `format=json` picks
# the rendering and every other value, a typo included, is ignored. `clamped` names a numeric
# one and carries its bounds.
#
# The ceiling is read from the tree rather than from the served schema, which is the opposite
# of what this file does everywhere else and is deliberate: /rooms' `limit` is an *advisory*
# parameter, so by the input doctrine it publishes no `minimum`/`maximum` at all — bounds in
# a schema say a value outside them is refused, and this one clamps (test_input_doctrine.py
# asserts their absence). store.MAX_LIMIT is a plain constant rather than a config knob, so
# the checkout being deployed is an exact source for it, and the alternative is a second copy
# of the number in JS.
ROOMS_KEY_MATCH = {"format": "json"}


def rooms_key() -> dict:
    """The /rooms cache-key spec, with the ceiling taken from the code being deployed."""
    return {
        "/rooms": {
            "match": dict(ROOMS_KEY_MATCH),
            "clamped": {"limit": {"min": 1, "max": store.MAX_LIMIT}},
        }
    }


def asset_name(path: str) -> str:
    """Where a URL path is stored under public/.

    `/` becomes index.html because that is the one filename an asset server resolves the
    root to; everything else is stored at its own path verbatim so the Worker can look it
    up with the request URL unchanged.
    """
    return "index.html" if path == "/" else path.lstrip("/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://technocore.chat")
    ap.add_argument("--out", default=str(pathlib.Path(__file__).parent / "public"))
    ap.add_argument(
        "--manifest", default=str(pathlib.Path(__file__).parent / "src" / "routing.json")
    )
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    types: dict[str, str] = {}
    failed: list[str] = []

    for path in PATHS:
        url = args.base.rstrip("/") + path
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                if r.status != 200:
                    failed.append(f"{path} -> HTTP {r.status}")
                    continue
                body = r.read()
                ctype = r.headers.get("content-type", "application/octet-stream")
        except Exception as exc:  # noqa: BLE001 - any failure is a failed snapshot
            failed.append(f"{path} -> {type(exc).__name__}: {exc}")
            continue

        dest = out / asset_name(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        types[path] = ctype
        print(f"  {path:44} {len(body):>7}B  {ctype}")

    repo = pathlib.Path(__file__).resolve().parent.parent
    for path, source in STATIC_FIRST.items():
        if path not in types:
            continue  # its fetch failed; the error below is the one worth reporting
        body = (repo / source).read_bytes()
        (out / asset_name(path)).write_bytes(body)
        print(f"  {path:44} {len(body):>7}B  <- {source} (static-first, from the tree)")

    if failed:
        # Fail loudly and write nothing further. A partial snapshot deployed as a fallback
        # is worse than no fallback: it answers some paths and 503s the rest, and which is
        # which depends on what happened to be up when this ran.
        print("\nsnapshot FAILED:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
        return 1

    edge_key = rooms_key()
    unspecified = sorted(set(EDGE_REVALIDATE) - set(edge_key))
    if unspecified:
        # Fail closed. Without a key spec the Worker would have to key on the raw URL, which
        # is the multiplication bug above — a deploy that silently did that is worse than one
        # that stops here, because nothing downstream would report it.
        print(f"\nsnapshot FAILED: no cache-key spec for {unspecified}", file=sys.stderr)
        return 1

    pathlib.Path(args.manifest).write_text(
        json.dumps(
            {
                "types": types,
                "static_first": sorted(STATIC_FIRST),
                "edge_cached": EDGE_CACHED,
                "edge_revalidate": EDGE_REVALIDATE,
                "edge_key": edge_key,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\n{len(types)} documents -> {out}")
    print(f"routing policy   -> {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
