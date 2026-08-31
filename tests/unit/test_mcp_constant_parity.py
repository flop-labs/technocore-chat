"""The constants the wrapper restates, pinned to the ones the service enforces.

`technocore-mcp` is a separate distribution: `uvx technocore-mcp` installs `mcp` and
`cryptography` and nothing from this repo, so the wrapper cannot import `store`, `config`
or `didkey` — not as a style choice but because they are not there. What it needs from
them it therefore spells out again, and a second spelling is a thing that can drift.

Drift here is quiet and it is not symmetric. A wrapper pattern *looser* than the
service's advertises calls the service will refuse, which surfaces as a confusing 400
rather than a schema error. A pattern *tighter* refuses calls the service would have
answered, and the caller never learns the service would have said yes. Neither shows up
in tests/test_mcp.py, which checks the wrapper against its own constants — self-consistent
and blind to exactly this.

So: this file, not an import and not a runtime fetch, and the reasons are worth writing
down because "just read it from /config" is the obvious idea and it is wrong three times
over. `/config` does not publish these — it carries the operational knobs (rate limits,
waiter slots, dupe filter) and neither the name grammar nor the limit ceiling. Tool
schemas must be answerable at `initialize`, before any tool has run, so fetching would
put a network round-trip in the handshake and make `tools/list` fail whenever the origin
is unwell. And the input doctrine (docs/design.md §3.5) says advisory parameters are
clamped by the service and refused by nobody, which makes the `1-200` in a description
documentation rather than enforcement — stale is harmless there *by design*, which is
what makes an assertion the right tool and a lookup the wrong one.

The runtime value, where a caller genuinely needs it, is already handled the other way:
`wait_for_message`'s own description points at `read_docs('config')` for `max_wait`
rather than promising the public instance's 10.

This file imports both sides and ships in neither. Nothing here may be added to
`mcp/pyproject.toml`; if it ever needs to be, the wrapper has stopped being standalone.

Run: uv run --group dev python -m pytest tests/unit/test_mcp_constant_parity.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mcp" / "src"))


@pytest.fixture(scope="module")
def schemas():
    """The tool schemas a client actually receives, keyed by tool name."""
    from technocore_mcp import server as wrapper

    async def collect():
        tools = await wrapper.server.list_tools()
        # The SDK has spelled this both ways across versions; the schema is the point.
        return {t.name: getattr(t, "inputSchema", None) or t.input_schema for t in tools}

    return anyio.run(collect)


def test_the_name_grammar_is_the_services_own_string():
    """One grammar, two spellings, byte-identical — including the anchors.

    `store.NAME_RE` is already written anchored, so this is plain equality rather than
    the reconstruction the did:key patterns below need.
    """
    from technocore_mcp import server as wrapper

    import store

    assert wrapper.NAME_PATTERN == store.NAME_RE.pattern


def test_the_did_and_signature_patterns_are_the_services_plus_anchors():
    """The wrapper's copies are the service's, anchored — and the anchors are load-bearing.

    The service applies these with `re.fullmatch`, where anchoring is implicit and writing
    `^...$` would be noise. The wrapper publishes them as JSON Schema `pattern`, which is
    defined as a *search*: unanchored, `did:key:z6Mk...` would match anywhere in the
    string, so a did with trailing garbage would pass the schema and reach the service.

    That is why "just import didkey.DID_PATTERN" would be a bug rather than a cleanup, and
    why the relationship asserted here is `^` + the service's + `$` rather than equality.
    """
    from technocore_mcp import server as wrapper

    import didkey

    assert wrapper.DID_PATTERN == f"^{didkey.DID_PATTERN}$"
    assert wrapper.SIG_PATTERN == f"^{didkey.SIG_PATTERN}$"


@pytest.mark.parametrize("tool", ["read_room", "list_rooms"])
def test_the_limit_ceiling_named_in_a_description_is_the_one_the_store_clamps_to(schemas, tool):
    """`clamped to 1-200` has to mean `store.MAX_LIMIT`, or the sentence misinforms.

    Asserted as substring presence rather than by parsing the prose: the number is the
    contract, the sentence around it is free to be rewritten, and a regex over English is
    a test that breaks on punctuation. If MAX_LIMIT moves, this fails and the description
    gets updated with it — which is the whole job.
    """
    import store

    description = schemas[tool]["properties"]["limit"]["description"]
    assert str(store.MAX_LIMIT) in description, description


@pytest.mark.skipif(
    "CHAT_MAX_WAIT" in os.environ,
    reason="the service's ceiling is a knob; this pins the default the wrapper assumes",
)
def test_the_wait_ceiling_matches_the_services_default():
    """`WAIT_CEILING` is what the wrapper assumes an unconfigured instance allows.

    Skipped when `CHAT_MAX_WAIT` is set, because then the two are *supposed* to differ:
    the wrapper's constant describes the public instance, and `wait_for_message` tells
    callers to read `read_docs('config')` for the instance in front of them. What must not
    drift is the default.
    """
    from technocore_mcp import server as wrapper

    import config

    assert wrapper.WAIT_CEILING == config.MAX_WAIT
