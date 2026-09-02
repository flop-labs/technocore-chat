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


def test_every_routed_path_is_snapshotted():
    """The direction that actually breaks: routed but not stored means the fallback has
    nothing to serve, so the Worker hands back the origin's 503 — on the one path someone
    added to the route list specifically so it would survive an outage."""
    assert _wrangler_routes() - set(_snapshot_module().PATHS) == set()


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
