"""Run: uv run --group dev python -m pytest tests

`/config` publishes what this deployment is set to. Two things have to hold for that to be
worth serving at all, and they pull in opposite directions: the numbers must be the ones
the handlers actually enforce (a published setting that disagrees with behaviour is worse
than none, because a machine reader believes it), and the document must never carry a
credential, a host path or the header this origin trusts for client identity. So the tests
here are mostly completeness checks against `src/config.py` itself: a knob added there is
published or withheld *by name*, never forgotten into the open.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import _client

client = _client.client  # the shared TestClient fixture

SRC = Path(__file__).resolve().parents[2] / "src"
# Every environment variable config.py reads, found in the file rather than listed here —
# a list would be the thing that goes stale, which is the failure these tests exist to
# catch. Quoted names only, so the CHAT_* spelled out in the prose comments beside them
# (there are dozens) do not count as knobs.
KNOBS = frozenset(
    re.findall(r"[\"'](CHAT_[A-Z_0-9]+|WEB_CONCURRENCY)[\"']", (SRC / "config.py").read_text())
)


def test_every_knob_is_either_published_or_withheld_by_name(client):
    """The completeness rule, and the reason this endpoint can be served unauthenticated.

    A new knob lands in config.py and is, by default, in neither set — which fails here
    rather than quietly appearing in a public document or quietly missing from it. The
    withheld half is not decoration: it is what lets a reader tell "deliberately not
    published" from "nobody thought about it", and an operator hunting for a setting gets
    the reason instead of silence.
    """
    doc = client.get("/config").json()
    published = {f"{doc['env_prefix']}{key.upper()}" for key in doc["settings"]}
    withheld = set(doc["withheld"])

    assert not (published & withheld), "a knob cannot be both published and withheld"
    missing = KNOBS - published - withheld
    assert not missing, f"config.py reads {sorted(missing)} and /config classifies neither way"
    invented = (published | withheld) - KNOBS
    assert not invented, f"/config names {sorted(invented)}, which config.py does not read"
    assert all(reason.strip() for reason in doc["withheld"].values()), "a reason, not a shrug"


def test_every_published_key_is_the_environment_variable_that_moves_it(client):
    """The document's whole schema is `CHAT_ + key.upper()`, so it has to be exactly true.

    It is what makes the answer useful to the person who has to *change* one of these: a
    caller reads the number, an operator reads the name of the knob that moves it, and
    neither has to be told the mapping twice. A key that did not resolve to a real variable
    would send that operator to edit something that does not exist.
    """
    import config

    doc = client.get("/config").json()
    assert doc["env_prefix"] == "CHAT_"
    for key, value in doc["settings"].items():
        name = f"CHAT_{key.upper()}"
        assert name in KNOBS, f"{key} publishes as {name}, which config.py never reads"
        assert getattr(config, key.upper()) == value, f"{key} is not the binding it claims"
    # Prose that names a number is prose that can disagree with it; every setting gets its
    # unit from the same place instead.
    assert set(doc["units"]) == set(doc["settings"]), "every setting states its unit"


def test_the_published_values_are_the_ones_this_process_enforces(client):
    """Read from `config` at request time, not captured at import: the point of the endpoint
    is that it answers for the process serving it. `config.override` is how a test moves a
    knob and is how a deployment differs from the defaults — if the document did not follow
    it here, it would not follow the environment there either."""
    import config

    with config.override(RATE_READ=7, MAX_WAIT=2.5, DUPE_FILTER_SECONDS=45.0, FSYNC=False):
        settings = client.get("/config").json()["settings"]
        assert settings["rate_read"] == 7
        assert settings["max_wait"] == 2.5
        assert settings["dupe_filter_seconds"] == 45
        assert settings["fsync"] is False

    # …and back, because a document that latched the first value it saw would pass the
    # assertions above and still be wrong for every request after a restart.
    assert client.get("/config").json()["settings"]["rate_read"] == config.RATE_READ


def test_the_rate_limits_it_publishes_are_the_ones_agent_json_publishes(client):
    """Two documents, one binding. The manifest is what registries read and /config is what
    a client tunes itself with, so the pair is only safe while neither can drift — which is
    true exactly because both are generated from `config` rather than written down."""
    import config

    with config.override(RATE_READ=41, RATE_WRITE=17, RATE_ROOMS_PER_DAY=3):
        settings = client.get("/config").json()["settings"]
        limits = client.get("/.well-known/agent.json").json()["limits"]
        assert settings["rate_read"] == limits["reads_per_minute_per_ip"] == 41
        assert settings["rate_write"] == limits["writes_per_minute_per_ip"] == 17
        assert settings["rate_rooms_per_day"] == limits["new_rooms_per_day_per_ip"] == 3


def test_no_credential_host_detail_or_trust_boundary_is_ever_in_the_body(client):
    """The endpoint is world-readable, so the withheld set is a security property, not a
    style choice. The stats token is a credential; CHAT_ROOT is where the host keeps the
    data; the client-IP header is the one header this origin trusts, and naming it tells
    anyone who can reach the origin directly which header to forge for a fresh rate-limit
    identity. Each is *set to a distinctive value* here rather than merely absent — an
    empty default would let this pass against a document that published all three.
    """
    import config

    with config.override(
        STATS_TOKEN="tok-must-not-appear",
        CLIENT_IP_HEADER="cf-connecting-ip",
        CORS_ORIGINS=["https://staging.internal.example"],
        SECURITY_CONTACT="ops@internal.example",
        DEBUG=3,
    ):
        body = client.get("/config").text
        for secret in (
            "tok-must-not-appear",
            "cf-connecting-ip",
            "staging.internal.example",
            "ops@internal.example",
            str(config.ROOT),
        ):
            assert secret not in body, f"{secret!r} reached a public document"
        # Not even as a boolean: whether a token is configured is the answer /stats' 404
        # exists to withhold, and "configured: false" would hand it over in one fetch.
        doc = json.loads(body)
        assert "stats_token" not in doc["settings"]
        assert doc["settings"]["rate_read"] == config.RATE_READ, "the rest still answers"


def test_it_is_never_rate_limited_and_stays_indexable(client):
    """Same reason the manual and the spec are free: a client that must read the limits to
    pace itself cannot be refused for reading them, and a 429 on the description of the
    throttle is a deadlock. The 429 body and the manual both name it in FREE_PATHS, so the
    claim has to be true of the running route."""
    import config
    import limit

    assert "/config" in limit.FREE_PATHS
    with config.override(RATE_READ=1, RATE_WRITE=1):
        for _ in range(5):
            response = client.get("/config")
            assert response.status_code == 200
    assert "x-robots-tag" not in response.headers, "it is documentation, not room content"
    assert response.headers["cache-control"] == "public, max-age=3600"


def test_a_published_setting_is_a_number_json_can_carry(client):
    """Publishing a knob makes its finiteness a contract.

    The two cache windows were parsed with a bare `float()`, which accepts `inf` and `nan`
    where the `int()` beside them raises — harmless while nothing published them, a broken
    document the moment something did: Python emits the bare token `Infinity`, RFC 8259
    forbids it, and every strict parser rejects the whole response. `inf` on a cache window
    was a live bug in its own right, too — the entry never expires and the view never
    refreshes again — so the process refuses to start instead, the way CHAT_MAX_WAIT
    already did.
    """
    for knob in ("CHAT_ROOMS_CACHE_SECONDS", "CHAT_NOTE_STATS_CACHE_SECONDS"):
        boot = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); import app"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", knob: "inf"},
        )
        assert boot.returncode != 0, f"app booted with a non-finite {knob}"
        assert "must be a finite number" in boot.stderr

    raw = client.get("/config").text
    assert "Infinity" not in raw and "NaN" not in raw
    json.loads(raw, parse_constant=_no_constants)


def _no_constants(token: str):
    raise AssertionError(f"the document carries the non-JSON token {token!r}")


def test_the_documents_that_should_point_at_it_do(client):
    """A document nothing links to is a document nobody finds. The manual names it where a
    throttled agent looks for the numbers, the manifest lists it beside the openapi, the
    spec describes it (the spec/app consistency check in test_docs.py enforces that), and
    the sitemap invites a crawler to it."""
    manual = client.get("/llms.txt").text
    assert "/config" in manual
    assert (
        client.get("/.well-known/agent.json").json()["documentation"]["config"].endswith("/config")
    )
    assert "/config" in client.get("/sitemap.xml").text
    assert client.get("/openapi.json").json()["paths"]["/config"]["get"]["operationId"]


def test_it_is_a_read_and_says_so(client):
    """One method, and the 405 names it — the same contract every other document route has.
    A POST here would be a caller trying to *set* something, which no endpoint on this
    service does; the refusal has to be unambiguous rather than a 404 that reads like a
    typo."""
    refused = client.post("/config", json={"rate_read": 1})
    assert refused.status_code == 405
    assert refused.headers["allow"] == "GET, HEAD"
    assert client.get("/config").status_code == 200
