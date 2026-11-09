"""The input doctrine (docs/design.md §3.5), enforced by generation rather than by review.

Every drift this catches was first reported by somebody pointing a fuzzer at a deployed
instance and reading the diff between the published schema and the answers: `limit`/`format`
bounds nobody enforced (#372/#402), `from`/`text` `str()`-coerced against a `"type": "string"`
schema (#427), a missing `from` refused with a *room*-name error (#373). A doctrine that is
only written down drifts again the next time a parameter is added; this makes the document
and the handlers each other's test.

In-process against the ASGI app, and against the app's *own* generated `/openapi.json` — so
there is no committed copy of the contract to go stale, and a schema change and a handler
change are checked against each other in the same run. The out-of-process twin, over a real
uvicorn socket, is the `contract` job in .github/workflows/ci.yml.
"""

from __future__ import annotations

import pytest
import schemathesis

import app as app_module
import config
import store

# Deterministic on purpose: a fixed seed plus `deterministic` fixes the generation and the
# pinned schemathesis fixes the generator, so a red run here is a change in this service and
# not in the tool that found it. `mode: all` runs positive and negative generation — the
# negative half is what the doctrine's refuse-class is for, and the whole point of the check
# list below. The example budget is the wall-clock knob: at 50 per operation the module runs
# in well under a minute inside the ordinary `pytest tests` job.
_CONFIG = schemathesis.Config.from_dict(
    {
        "seed": 0,
        # `allow-extra-parameters` off: the negative generator's other trick is to add a
        # query parameter the document does not list and expect a refusal. This service
        # ignores unknown query parameters by design — `?n=` exists precisely so a caller
        # can bolt junk onto a URL to defeat a harness cache — so that mutation tests a
        # promise the contract has never made.
        "generation": {
            "mode": "all",
            "max-examples": 50,
            "deterministic": True,
            "allow-extra-parameters": False,
        },
        "checks": {
            # Everything on, then the exclusions, each with the reason it can never pass
            # here rather than a reason it is inconvenient. A check that is off by default
            # and never revisited is how the drift this file exists to catch gets in.
            "enabled": True,
            # Valid input is legitimately refused all over this service: a write to a `mb-`
            # mailbox needs a signature, a reserved namespace refuses everybody, and the
            # duplicate filter answers 422 to a second identical message. All schema-valid,
            # all correctly non-2xx.
            "positive_data_acceptance": {"enabled": False},
            # Needs an `Authorization` scheme to strip. This service has no credentials at
            # all, so the check has nothing to do and reports that as a warning every run.
            "ignored_auth": {"enabled": False},
            # Both need OpenAPI links to walk a create/delete lifecycle. Nothing here
            # publishes links: rooms and notes are created by writing to them and are never
            # deleted by a caller.
            "use_after_free": {"enabled": False},
            "ensure_resource_availability": {"enabled": False},
        },
        # `?wait=` holds the connection open for real, and the ceiling is published, so a
        # generated long poll at the default 10s ceiling would spend the whole budget
        # waiting. One second is still a wait; CHAT_MAX_WAIT is overridden to match below.
        "phases": {"stateful": {"enabled": False}},
    }
)

schema = schemathesis.openapi.from_asgi("/openapi.json", app_module.app, config=_CONFIG)


@pytest.fixture(scope="module", autouse=True)
def instance(tmp_path_factory):
    """A fresh store per run, with the abuse budgets raised out of the way.

    Generated traffic is exactly the shape the limiter exists to refuse, and a run that is
    mostly 429s conforms perfectly while checking nothing: a refusal the fuzzer never gets
    past is a handler it never reached. These are the shipped `CHAT_ROOT`,
    `CHAT_RATE_READ`, `CHAT_RATE_WRITE`, `CHAT_RATE_ROOMS_PER_DAY` and `CHAT_MAX_WAIT`
    knobs, moved for this module only; everything the checks assert on is unaffected by
    them. `CHAT_DUPE_FILTER_SECONDS` is pinned off for the same reason it is in the shared
    client fixture — the filter is its own tested contract, not this one.

    Module-scoped, so the store the generator fills stays filled for the whole run: a room
    the fuzzer wrote in one operation is a room the read operations can then find, and
    hypothesis refuses a function-scoped fixture behind `@given` for exactly the reason it
    would be wrong here — it is not reset per example, and must not be.

    The three memo caches behind /rooms are process state that outlives a ROOT, so they are
    cleared for the same reason the shared client fixture clears them: an entry keyed on a
    previous run's store would answer a generated read with a room this one never wrote.
    Their clock is deliberately not pinned here — validity is part of the key now, so a
    window boundary falling mid-run changes which entry is looked up and nothing else, and
    no check in this module asserts on cache hits.
    """
    app_module._buckets.clear()
    app_module._rooms_walk.cache_clear()
    store._cached_window.cache_clear()
    store._topics_memo.cache_clear()
    app_module._identities.clear()
    with config.override(
        ROOT=tmp_path_factory.mktemp("contract"),
        RATE_READ=1_000_000,
        RATE_WRITE=1_000_000,
        RATE_ROOMS_PER_DAY=1_000_000,
        MAX_WAIT=1.0,
        DUPE_FILTER_SECONDS=0,
    ):
        yield


@schema.parametrize()
def test_the_service_answers_what_its_openapi_document_promises(case):
    """No 5xx, no undocumented status, every body matching the schema for the status it
    came back with — and, the half this doctrine added, schema-invalid input never
    answering 2xx.

    That last check (`negative_data_rejection`) failed before the doctrine sweep and passes
    after it, in both directions: the advisory parameters stopped publishing `minimum`/
    `maximum`/`enum` constraints the handler only clamps, and the semantic ones started
    refusing the types and combinations the schema had always said were the only legal
    ones. Either half alone leaves the check red, which is precisely why they are one
    change.
    """
    case.call_and_validate()
