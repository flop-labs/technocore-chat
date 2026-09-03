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
# snapshotted: like /healthz these are live figures, and a stored answer would outlive the
# service that produced it.
#
# Not because the walk is expensive: bench/rooms.py measures it in the hundreds of
# milliseconds, less than a tenth of which is what `limit` buys. Against concurrent writers it
# costs an order of magnitude more at *every* limit, including the one that reads no room
# tails. The cost is queueing, not work, so no cache window can be made reliably longer than
# it and walking less does not help. Serving the copy and refreshing behind ctx.waitUntil()
# is what stops a reader ever paying it.
#
# The interval is short because /rooms is activity monitoring: `idle_seconds` and `last_seq`
# are the payload, and a stale copy misreports exactly what a reader came for. It has a floor
# rather than a target — test_the_refresh_interval_is_not_faster_than_the_origin_can_answer.
EDGE_REVALIDATE = {"/rooms": 5}

# What a /rooms cache key is made of. The handler reads only `limit` and `format` and clamps
# the first, so /rooms?limit=999999999, ?limit=200 and ?limit=200&x=1 are one reply — and a
# key built from the raw URL stores them as three, letting a caller force a cold walk per
# request by incrementing a digit. app.py fixed this for its own cache ("the key space is the
# reply space"); a lane in front of it has to carry the same fix.
#
# `match` names a parameter that matters only when it equals one value: `format=json` picks
# the rendering, every other value is ignored. `clamped` names a numeric one and its bounds.
#
# The ceiling is restated here rather than read from anywhere, which needs two excuses. It is
# not taken from the served schema because `limit` is advisory: by the input doctrine it
# publishes no minimum/maximum, since bounds mean refusal and this clamps
# (test_input_doctrine.py asserts their absence). And it is not imported from store because
# this file runs under bare python3 from deploy.sh and store pulls in the service's whole
# dependency chain. Drift is caught instead — the edge-key test asserts it against
# store.MAX_LIMIT, which is where the number actually lives.

# Paths the edge owns outright: the origin serves nothing at them, so unlike everything else
# here the stored bytes are not a copy of a live answer — they are the only answer. That is
# why they are neither snapshotted (there is nothing to fetch) nor origin-first (there is
# nothing to prefer), and why the Worker 404s a missing one rather than proxying: falling
# through to the origin would only reproduce the 404 this lane exists to stop.
#
# /favicon.ico is requested by every browser that opens /humans, whether or not the page asks
# for one, and each of those was reaching the origin to be refused. The bytes are drawn by
# edge/make_favicon.py and tracked; the content type is stated because the manifest is what
# the Worker reads, and an asset server's guess is not something this file leaves to chance.
EDGE_ONLY = {"/favicon.ico": ("edge/assets/favicon.ico", "image/x-icon")}

ROOMS_KEY_MATCH = {"format": "json"}


def rooms_key() -> dict:
    """The /rooms cache-key spec, with the ceiling taken from the code being deployed."""
    return {
        "/rooms": {
            "match": dict(ROOMS_KEY_MATCH),
            "clamped": {"limit": {"min": 1, "max": 200}},
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

    for path, (source, ctype) in EDGE_ONLY.items():
        body = (repo / source).read_bytes()
        (out / asset_name(path)).write_bytes(body)
        types[path] = ctype
        print(f"  {path:44} {len(body):>7}B  <- {source} (edge-only, from the tree)")

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
                "edge_only": sorted(EDGE_ONLY),
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
