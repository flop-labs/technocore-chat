"""Run: uv run --group dev python -m pytest tests

The edge Worker describes the same document surface in three places — the route list it is
attached to, the paths it snapshots, and the routes the app actually serves. Three hand-kept
lists of the same thing is drift waiting to happen, and the failure is silent in the worst
direction: a path routed to the Worker but never snapshotted answers 503 during exactly the
outage the Worker exists for.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import _client
import pytest

import store

EDGE = pathlib.Path(__file__).resolve().parents[2] / "edge"

client = _client.client  # the shared TestClient fixture


def _snapshot_module():
    spec = importlib.util.spec_from_file_location("edge_snapshot", EDGE / "snapshot.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wrangler_routes() -> set[str]:
    """The path half of every route in wrangler.jsonc, as a URL path."""
    raw = (EDGE / "wrangler.jsonc").read_text(encoding="utf-8")
    stripped = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
    config = json.loads(stripped)
    paths = set()
    for route in config["routes"]:
        _, _, path = route["pattern"].partition("/")
        paths.add("/" + path)
    return paths


def test_every_snapshotted_path_is_routed_to_the_worker():
    """A snapshot of a path the Worker never sees is dead weight in the bundle."""
    assert set(_snapshot_module().PATHS) - _wrangler_routes() == set()


def test_every_routed_path_is_either_snapshotted_or_deliberately_not():
    """The direction that actually breaks: routed but not stored means the fallback has
    nothing to serve, so the Worker hands back the origin's 503 — on the one path someone
    added to the route list specifically so it would survive an outage.

    The edge-cached lane is the deliberate exception, and has to be named rather than
    subtracted silently: those paths are routed, are never snapshotted, and that is the
    whole point of them.
    """
    snapshot = _snapshot_module()
    accounted = set(snapshot.PATHS) | set(snapshot.EDGE_CACHED) | set(snapshot.EDGE_REVALIDATE)
    assert _wrangler_routes() - accounted == set()


def test_a_liveness_path_is_never_snapshotted():
    """The invariant the third lane exists to hold.

    A stored copy of /healthz is a file that says "ok". The only occasion it would ever be
    served is the one where the origin cannot answer — so the single thing it can do is
    report a service that is gone as healthy, to every monitor watching it.
    """
    snapshot = _snapshot_module()
    live = set(snapshot.EDGE_CACHED) | set(snapshot.EDGE_REVALIDATE)
    assert live & set(snapshot.PATHS) == set()
    assert live & set(snapshot.STATIC_FIRST) == set()
    assert "/healthz" not in snapshot.PATHS
    assert "/rooms" not in snapshot.PATHS


def test_the_edge_cache_window_is_long_enough_to_be_worth_having():
    """1s would mostly miss: Cloudflare caches per PoP, so the per-PoP arrival rate is far
    below the global one, and a window shorter than the gap between two requests at the same
    PoP caches nothing. Pinned so a later "make it fresher" edit has to argue with the
    reason rather than silently neutering the lane."""
    for path, seconds in _snapshot_module().EDGE_CACHED.items():
        assert seconds >= 5, f"{path} at {seconds}s is below the per-PoP arrival gap"


def test_the_worker_covers_every_document_route_the_app_serves():
    """And the surface itself: a document route added to app.py has to reach this Worker,
    or it silently keeps the old origin-only behaviour. Discovered from the running app
    rather than from a fourth list.
    """
    import app as app_module

    served = set()
    for route in app_module.app.routes:
        path = getattr(route, "path", "")
        # Document routes only: the dynamic lanes carry path params and are deliberately
        # never routed to the Worker (see wrangler.jsonc).
        if not path or "{" in path:
            continue
        if path in ("/healthz", "/stats", "/rooms"):
            continue  # liveness, token-gated, and the one read that is not a document
        served.add(path)

    missing = served - set(_snapshot_module().PATHS)
    assert not missing, f"document routes not covered by the edge Worker: {sorted(missing)}"


def test_the_generated_routing_manifest_agrees_with_the_snapshot_policy():
    """`src/routing.json` is written by snapshot.py at deploy time and gitignored, so this
    is a local check after a snapshot rather than a CI gate — the CI-visible guards are the
    three list comparisons above, which read the sources rather than the artefact.

    It is here because the Worker imports this file and trusts both halves: a path stored
    without a type gets served as whatever the asset server guessed (wrong for the six with
    no file extension), and a static_first entry that drifted from snapshot.py would put a
    configuration-carrying document into the lane that never asks the origin.
    """
    manifest = EDGE / "src" / "routing.json"
    if not manifest.is_file():
        pytest.skip("no snapshot in this tree; run edge/snapshot.py")
    routing = json.loads(manifest.read_text(encoding="utf-8"))
    snapshot = _snapshot_module()
    assert set(routing["types"]) == set(snapshot.PATHS)
    assert set(routing["static_first"]) == set(snapshot.STATIC_FIRST)
    assert all(v and "/" in v for v in routing["types"].values()), routing["types"]


# The knobs that reach the document surface, moved far enough that anything quoting one has
# to change. Legal values, nothing like the defaults.
_MOVED = {
    "MAX_ROOMS": 999_331,
    "MAX_NOTES_PER_NS": 999_337,
    "MAX_NOTES_TOTAL": 9_999_991,
    "RATE_READ": 977,
    "RATE_WRITE": 971,
    "DUPE_FILTER_SECONDS": 967.0,
    # The one that caught /robots.txt: manifest.robots_txt embeds an absolute Sitemap URL
    # built from this, so a document quoting it is configuration-dependent however static
    # its prose looks. Omitting it is what let robots.txt sit in the static lane.
    "PUBLIC_URL": "https://moved.example",
}


def test_the_static_first_documents_are_served_verbatim_from_their_assets(client):
    """The claim the static lane rests on: these documents are files, not renderings.

    /skill.md and /patterns.md are `_asset()` reads served unchanged, so a stored copy can
    never disagree with the origin about configuration — there is no substitution step in
    which configuration could enter. Asserted against the bytes on disk rather than
    described, because the day someone templates a knob into the manual is the day the edge
    starts publishing whatever that knob was at the last upload.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    for path, asset in (
        ("/skill.md", root / "SKILL.md"),
        ("/patterns.md", root / "src" / "patterns.md"),
    ):
        assert client.get(path).text == asset.read_text(encoding="utf-8"), path


def test_robots_txt_moves_with_the_public_url_and_so_stays_origin_first(client):
    """Why /robots.txt is not in the static lane, though it reads like the most static
    document on the service.

    manifest.robots_txt builds an absolute `Sitemap:` URL, and _base_url prefers
    CHAT_PUBLIC_URL over the request Host — so moving the public origin, a compose-time
    change of exactly the kind this split exists to survive, changes these bytes. A stored
    copy would keep publishing the old origin.
    """
    import config

    before = client.get("/robots.txt").text
    with config.override(PUBLIC_URL="https://moved.example"):
        assert client.get("/robots.txt").text != before
    assert "/robots.txt" not in _snapshot_module().STATIC_FIRST


def test_the_origin_first_documents_really_do_carry_configuration(client):
    """The control. Without it the assertions above could pass by testing nothing.

    The manual is rendered once at import (`MANUAL = _render_manual()`), so a knob moved at
    runtime does not change the served bytes — which is exactly why these documents are
    origin-first rather than static-first: their content is fixed by the configuration the
    *process started with*, and a compose edit that changes a knob restarts the process. The
    renderer is therefore called directly here, to show the knobs reach the manual at all.
    """
    import app as app_module
    import config

    with config.override(**_MOVED):
        rerendered = app_module._render_manual()
    assert rerendered != app_module.MANUAL, (
        "the manual no longer quotes any configured value — if that is deliberate it can "
        "join STATIC_FIRST, and if it is not, the split above has lost its reason"
    )
    assert "999331" in rerendered.replace(",", "")


def test_the_static_first_set_is_a_subset_of_what_is_snapshotted():
    snapshot = _snapshot_module()
    assert set(snapshot.STATIC_FIRST) <= set(snapshot.PATHS)


def test_the_edge_cached_lane_shares_its_copy_only_with_the_edge():
    """A source assertion, deliberately, and narrow.

    There is no JS harness in this repo, and the difference this guards is behavioural
    rather than cosmetic: `s-maxage` is a shared-cache directive, so only Cloudflare holds
    the copy, while a bare `max-age` would let a browser, a monitoring client or a
    downstream proxy reuse `ok` without contacting the edge at all. That puts liveness
    staleness outside Cloudflare's control and beyond the reach of a purge — on the one
    endpoint whose entire job is to be current.
    """
    worker = (EDGE / "src" / "worker.js").read_text(encoding="utf-8")
    assert "s-maxage=${seconds}" in worker
    assert "max-age=0, s-maxage=${seconds}" in worker, (
        "the edge-cached copy must not carry a private max-age"
    )


def test_the_revalidating_lane_never_makes_a_reader_wait_for_the_origin():
    """The property the lane exists for, asserted on the source because there is no JS
    harness here — and it is the one a later edit would quietly remove.

    /rooms is an O(total-rooms) walk (technocore-chat#576) whose cost under concurrency is
    dominated by queueing rather than by the walk itself — bench/rooms.py separates the two.
    No cache window can be made reliably longer than a cost with no upper bound, so the reader
    who arrives after the window closes pays all of it *and* holds an anyio thread while doing
    so. Returning the stale copy unconditionally is what breaks that.
    """
    worker = (EDGE / "src" / "worker.js").read_text(encoding="utf-8")
    lane = worker[worker.index("async function revalidating(") :]
    lane = lane[: lane.index("export default")]
    assert "return hit;" in lane, "the cached copy must be returned whatever its age"
    assert "ctx.waitUntil(" in lane, "the refresh must not be awaited on the request path"
    assert "fill(" in lane, "a burst on one PoP must not queue one walk per reader"


def test_the_revalidating_lane_refreshes_less_often_than_the_cached_one():
    """A revalidate interval inside the edge-cached window is a path in the wrong lane.

    The two lanes differ in what they spend: EDGE_CACHED makes one reader wait for the origin
    each time its window closes, EDGE_REVALIDATE never makes anyone wait and pays for that
    with a copy that may be a whole interval old. Staleness is only worth buying for a path
    the cheaper lane cannot cover, so an interval as short as a cached window is evidence the
    path belongs in EDGE_CACHED instead.
    """
    snapshot = _snapshot_module()
    longest_cached = max(snapshot.EDGE_CACHED.values(), default=0)
    for path, seconds in snapshot.EDGE_REVALIDATE.items():
        assert seconds > longest_cached, (
            f"{path} refreshes every {seconds}s, inside the {longest_cached}s edge-cached "
            "window — a path refreshed that often belongs in EDGE_CACHED"
        )


def _between(text: str, start: str, end: str) -> str:
    """One function's source, for the assertions there is no JS harness to make properly."""
    body = text[text.index(start) :]
    return body[: body.index(end)]


def test_the_cold_fill_is_single_flighted_too():
    """The refresh was deduplicated from the start and the cold path was not — but a cold PoP
    is where a burst costs most, because there is no copy to serve and every reader would
    otherwise start its own walk at the origin whose thread pool this lane protects.
    """
    worker = (EDGE / "src" / "worker.js").read_text(encoding="utf-8")
    lane = _between(worker, "async function revalidating(", "export default")
    assert "fromOrigin(" not in lane, "the lane must reach the origin only through fill()"
    cold = lane[lane.index("if (!hit)") :]
    assert "fill(" in cold[: cold.index("\n")], "the cold path must join the shared fill"


def test_a_caller_specific_reply_never_becomes_the_shared_copy():
    """/rooms carries a budget footer once a caller's read allowance runs low, and the handler
    keeps that reply out of any shared cache on purpose (`return resp if note else
    _edge_cacheable(resp)`). This lane rewrites Cache-Control on whatever it stores, so
    without the check it would publish one caller's pacing to every reader of the key.
    """
    fill = _between(
        (EDGE / "src" / "worker.js").read_text(encoding="utf-8"),
        "async function fromOrigin(",
        "function fill(",
    )
    assert fill.index("no-store") < fill.index("caches.default.put"), (
        "the no-store check must come before the copy is stored, not after"
    )


def test_the_edge_hold_outlives_a_sustained_refresh_outage():
    """An expiry reachable while refreshes are failing breaks the one promise this lane makes.
    The copy would expire exactly when nothing can replace it and the next reader would be
    back on the walk — the failure the lane exists to remove, in the situation it was built
    for. The policy is that the copy survives a hundred consecutive failed refreshes.
    """
    worker = (EDGE / "src" / "worker.js").read_text(encoding="utf-8")
    found = re.search(r"EDGE_HOLD_SECONDS = (\d+)", worker)
    assert found, "the lane must declare how long the edge may hold a copy"
    hold = int(found.group(1))
    longest = max(_snapshot_module().EDGE_REVALIDATE.values(), default=0)
    assert hold >= longest * 100, f"a {hold}s hold does not outlive a {longest}s refresh lapse"


def test_every_revalidating_path_has_a_cache_key_spec():
    """Without one the Worker would have to key on the raw URL, which is the bug below."""
    snapshot = _snapshot_module()
    spec = snapshot.rooms_key()
    assert set(snapshot.EDGE_REVALIDATE) <= set(spec)


def test_the_edge_key_is_the_reply_space_and_not_the_url_space(client):
    """The edge copy is keyed on the clamped limit rather than on the raw query string. The
    ceiling comes from store.MAX_LIMIT, so assert it against the handler's actual behaviour:
    if the two ever parted, the edge would serve one caller's row count to another.

    This is the bug app.py fixed for its own cache — "?limit=200 and ?limit=1000000 are one
    reply and were two entries" — reappearing one layer out, so it is checked the same way.
    """
    rule = _snapshot_module().rooms_key()["/rooms"]
    limit = rule["clamped"]["limit"]
    assert limit["max"] == store.MAX_LIMIT
    assert rule["match"] == {"format": "json"}
    # The doctrine reason this number comes from the tree and not from the served schema:
    # an advisory parameter publishes no bounds, because bounds mean refusal and this clamps.
    schema = client.get("/openapi.json").json()["paths"]["/rooms"]["get"]["parameters"]
    published = next(p for p in schema if p["name"] == "limit")["schema"]
    assert "maximum" not in published and "minimum" not in published

    for name in ("edgekey-a", "edgekey-b", "edgekey-c"):
        client.get(f"/r/{name}/say/nick/hello")

    def listed(query: str) -> list[str]:
        payload = client.get(f"/rooms?format=json&{query}").json()
        return [r["room"] for r in payload["rooms"]]

    # Everything at or past the ceiling is one reply, which is what lets the key collapse it.
    assert listed(f"limit={limit['max']}") == listed(f"limit={limit['max'] * 100000}")
    # And zero means one, the handler's `or 1` — the edge arithmetic mirrors it exactly.
    assert listed("limit=0") == listed("limit=1")


def test_a_head_request_can_never_become_the_stored_body():
    """route() sends HEAD into this lane and cacheKey() normalises it to a GET key, so a fill
    that fetched the caller's own request would read a HEAD's empty body and store it under
    that GET key — and every later GET for the same canonical query would be served an empty
    /rooms until the copy was replaced, which at EDGE_HOLD_SECONDS is a day. There is no JS
    harness here, so this asserts the property on the source: the fill fetches the key, which
    is a GET by construction, and never the request it was handed.
    """
    origin = _between(
        (EDGE / "src" / "worker.js").read_text(encoding="utf-8"),
        "async function fromOrigin(",
        "function fill(",
    )
    assert "fetch(request" not in origin, "a fill must not fetch the caller's own request"
    assert 'new Request(key.url, { method: "GET"' in origin, (
        "the fill must fetch a GET of the canonical key it is about to write"
    )
    assert origin.index("const canonical") < origin.index("await fetch("), (
        "the canonical GET must be what is fetched, not built after the fact"
    )
