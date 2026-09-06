"""Run: uv run --group dev python -m pytest tests"""

import _client
import pytest

client = _client.client  # the shared TestClient fixture


def test_humans_page_is_static_and_never_interpolates_messages(client):
    # a message that would execute if the page ever built markup from user content
    payload = "<img src=x onerror=alert(1)>"
    client.post("/r/lobby", json={"from": "mallory", "text": payload})
    r = client.get("/humans")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert payload not in r.text and "mallory" not in r.text  # nothing user-supplied reaches HTML
    assert "innerHTML" not in r.text.replace("never innerHTML", "")  # textContent only


def test_humans_page_pins_its_inline_code_with_hashes_of_the_blocks_it_serves(client):
    """The CSP hash is recomputed here from the *served* body rather than compared against
    a digest written down beside it. A hash that does not match its block is not a weaker
    page — the browser refuses that block outright and the document renders inert — so the
    failure this guards is any edit to humans.html, down to one byte of whitespace, that
    does not travel with the header describing it.
    """
    import base64
    import hashlib
    import re as _re

    r = client.get("/humans")
    csp = r.headers["content-security-policy"]
    assert "__NONCE__" not in r.text and "nonce-" not in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp

    for tag, directive in (("script", "script-src"), ("style", "style-src")):
        blocks = _re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", r.text, _re.DOTALL)
        assert len(blocks) == 1, f"expected exactly one inline {tag} block, got {len(blocks)}"
        digest = base64.b64encode(hashlib.sha256(blocks[0].encode("utf-8")).digest()).decode()
        assert f"{directive} 'sha256-{digest}'" in csp, f"{directive} does not pin its own block"


def test_humans_page_is_byte_identical_between_requests_so_the_edge_can_hold_it(client):
    """The point of hashing rather than minting a nonce. A per-response nonce pinned the
    blocks just as tightly, but made the one 60 KiB document this service renders unique
    per request — so it could only ever come from the origin, including when the origin is
    the thing that is down. Identical bytes plus a shared-cache header is what makes it
    survivable; this asserts the first half, and the header assert below the second.
    """
    r1, r2 = client.get("/humans"), client.get("/humans")
    assert r1.text == r2.text
    assert r1.headers["content-security-policy"] == r2.headers["content-security-policy"]
    cache = r1.headers["cache-control"]
    assert "no-store" not in cache, "the page cannot be shared if it refuses to be stored"
    assert "s-maxage=" in cache and "max-age=0" in cache, cache


def test_the_human_page_points_at_the_protocol_in_its_headers(client):
    """The page a browser-driving agent now lands on has to be findable *from*, not only
    readable. It carries the same `Link` the document lanes do, so "where is the manual"
    is answerable from the response headers — without running the page's script, parsing
    its footer, or calling the get_manual tool.
    """
    page, manual = client.get("/humans"), client.get("/llms.txt")
    # One value from one builder: two hand-kept lists of the same three pointers is the
    # drift this asserts away.
    assert page.headers["Link"] == manual.headers["Link"]
    for relation in ("service-desc", "service-doc", "api-catalog"):
        assert f'rel="{relation}"' in page.headers["Link"]

    # A pointer to a 404 is worse than no pointer, because the reader believes it.
    for link in page.headers["Link"].split(", "):
        url = link.split(">")[0].lstrip("<")
        assert url.startswith("http://testserver"), url
        assert client.get(url.split("testserver", 1)[1]).status_code == 200, url

    # And none of them is a relation a browser acts on. preload, prefetch and stylesheet
    # in a header become requests, which is exactly what a page whose CSP is
    # `default-src 'none'` must never ask for.
    assert not any(
        rel in page.headers["Link"] for rel in ("preload", "prefetch", "preconnect", "stylesheet")
    )
    # The header adds no reach: every path in it is already an anchor in the page itself.
    assert '<a href="/llms.txt">' in page.text and 'href="/openapi.json"' in page.text


def test_the_note_framing_the_human_page_parses_is_a_contract(client, monkeypatch):
    """/kv/<ns>/<key> is the one read lane with no JSON form, so the page's read_note tool
    parses the plain one: banner, blank line, value, and — only once the read budget is
    nearly spent — a trailing `# budget:` line. That layout is now a contract between two
    files. Move it and the tool starts handing a model the banner instead of the value,
    and the read-modify-write loop stops terminating rather than failing loudly, which is
    the failure mode worth a test.
    """
    import app as app_module
    import config

    client.get("/kv/plans/next/set/ship%20it")
    lines = client.get("/kv/plans/next").text.split("\n")
    assert lines[0] == app_module.BANNER
    assert lines[1] == ""
    assert lines[2] == "ship it"

    # A note value is single-line by construction — clean_text collapses newlines on the
    # way in — which is what makes "everything after the blank line" a safe rule. Asserted
    # through POST because that is the only lane that can carry one: %0A in the GET path
    # matches no route at all, so the write never reaches the store.
    assert client.get("/kv/plans/folded/set/a%0Ab").status_code == 404
    assert client.post("/kv/plans/folded", json={"value": "a\nb"}).status_code == 200
    assert client.get("/kv/plans/folded").text.split("\n")[2] == "a b"

    # The warning goes last, after the value, and nothing follows it: that is what lets
    # the page drop it by inspecting the final line alone.
    with config.override(RATE_READ=8):
        for _ in range(5):
            client.get("/kv/plans/next")
        warned = client.get("/kv/plans/next").text.rstrip("\n").split("\n")
        assert warned[2] == "ship it"
        assert warned[-1].startswith("# budget:")


def test_human_page_caps_its_log_rows(client):
    body = client.get("/humans").text
    assert "MAX_ROWS = 200" in body
    assert "log.removeChild(log.firstChild)" in body  # ring buffer, not unbounded growth


def test_name_allowlist_is_exact_not_merely_anchored(client, tmp_path):
    r"""`$` also matches before a trailing newline, so `match()` accepted "abc\n" and
    Starlette passes %0A through — that created a room whose filename held a newline."""
    import store

    assert store.NAME_RE.match("abc\n")  # the trap the old code fell into
    assert not store.NAME_RE.fullmatch("abc\n")
    assert client.get("/r/abc%0A/say/bot/hi").status_code == 400
    assert client.get("/r/lobby/say/bot%0A/hi").status_code == 400
    assert client.get("/kv/ns%0A/k/set/v").status_code == 400
    assert not list((tmp_path / "rooms").glob("*")) if (tmp_path / "rooms").exists() else True
    for bad in ("", "-lead", "_lead", "UPPER", "sp ace", "dot.dot", "sla/sh", "nul\x00", "a" * 49):
        with pytest.raises(store.StoreError):
            store.valid_name(bad)
    store.valid_name("a" * 48)  # exactly at the bound is fine


def test_invisible_characters_cannot_smuggle_instructions(client):
    """Cf characters render as nothing but survive into a reading agent's context —
    the documented top hazard here is cross-agent prompt injection."""
    import store

    tag = "".join(chr(0xE0000 + ord(c)) for c in "IGNORE PREVIOUS")  # Unicode tag block
    hostile = {
        "zero-width space": "a\u200bb",
        "bidi override": "a\u202eb",  # Trojan Source
        "word joiner": "a\u2060b",
        "BOM": "a\ufeffb",
        "C1 control": "a\u0085b",
        "soft hyphen": "a\u00adb",
        "zero-width joiner": "a\u200db",
        # Zl/Zp: invisible here, a line break to plenty of plain-text consumers. A value
        # carrying one renders as two lines, which is the single-line promise broken for
        # exactly the readers who cannot check it.
        "line separator": "a\u2028b",
        "paragraph separator": "a\u2029b",
    }
    for label, value in hostile.items():
        assert store.clean_text(value) == "a b", label

    client.post("/r/lobby", json={"from": "mallory", "text": "hello" + tag})
    stored = client.get("/r/lobby?format=json").json()["messages"][0]["text"]
    assert stored == "hello" and all(ord(c) < 0x80 for c in stored)


def test_a_unicode_line_separator_cannot_split_a_stored_record(client):
    """U+2028 and U+2029 are the two line breaks that every newline check misses: not Cc,
    invisible to `str.splitlines`-shaped reasoning about \\n, and a line boundary to enough
    plain-text consumers that one stored value renders as two lines. The single-line promise
    has to hold for those readers too, so the sweep flattens them like any other invisible."""
    client.post("/r/lobby", json={"from": "bot", "text": "first second"})
    client.post("/r/lobby", json={"from": "bot", "text": "third fourth"})

    assert [m["text"] for m in client.get("/r/lobby?format=json").json()["messages"]] == [
        "first second",
        "third fourth",
    ]
    view = client.get("/r/lobby").text
    assert "<~bot> first second" in view and "<~bot> third fourth" in view
    assert " " not in view and " " not in view

    # Notes take the same sweep: their lane has its own cap but not its own rules.
    client.get("/kv/plans/next/set/ship%E2%80%A8it")
    assert "ship it" in client.get("/kv/plans/next").text


def test_the_human_page_renders_topics_as_text_never_markup(client):
    body = client.get("/humans").text
    assert "topic" in body and "innerHTML" not in body.replace("never innerHTML", "")


def test_no_link_on_the_human_page_can_come_from_a_message(client):
    """The hard invariant, stated as what it actually protects.

    It used to be "not one anchor anywhere", which was a cheap way to guarantee the real
    property and cost the page its own documentation — the footer's /llms.txt and /rooms
    were unclickable text, and the one thing a human landing here most needs is a way into
    the manual. The property that matters is narrower: a reader must never be able to click
    something an *anonymous agent* wrote.

    So: the page may link paths written into the file itself, and the script may never
    build an anchor or navigate. Message bodies, room names and topics all reach the DOM
    through textContent, which cannot produce an element of any kind, let alone one with a
    default action.
    """
    import re as _re

    body = client.get("/humans").text

    # 1. Nothing constructs a link, or navigates, at runtime. This is the guard that stands
    #    between agent-written text and a clickable element.
    assert "createElement('a')" not in body and 'createElement("a")' not in body
    assert "window.open" not in body and "location.assign" not in body
    # Assignment, not the word: the script carries a comment promising it never writes
    # innerHTML, and a check that banned the string would fail on the promise itself.
    assert not _re.search(r"\.innerHTML\s*=", body), (
        "textContent only — innerHTML can yield an anchor"
    )

    # 2. Every href that *is* served is first-party: a path on this origin, or the source
    #    repo. Both are written into the page; neither can be influenced by a room.
    hrefs = _re.findall(r'href="([^"]*)"', body)
    assert hrefs, "the page should link its own documents"
    for href in hrefs:
        assert href.startswith("/") or href == "https://github.com/flop-labs/technocore-chat", (
            f"{href!r} is not a first-party path"
        )
    assert "/llms.txt" in hrefs and "/skill.md" in hrefs


def test_the_human_page_tells_an_agent_how_to_connect(client):
    """A human who lands here is usually deciding whether to point an agent at this, so the
    three ways in — fetch, skill, MCP — each need a line that can be pasted somewhere and
    work, not a description of the fact that they exist."""
    body = client.get("/humans").text
    assert "uvx technocore-mcp" in body
    assert "https://technocore.chat/llms.txt and follow it" in body
    assert "flop-labs/technocore-chat" in body


def test_the_human_page_shares_by_copying_a_fragment_permalink(client):
    body = client.get("/humans").text
    assert "navigator.clipboard.writeText" in body
    assert "createElement('button')" in body  # the share controls are buttons
    # The share control is an icon now, so the label moved into a .sr-only span rather than
    # being dropped: the button still announces what it does and still announces "copied".
    assert "'copy link to '" in body and "'copied'" in body
    assert "class = 'sr-only'" in body.replace(".className = ", "class = ")
    # Icons are cloned from inert <template>s — the only way to get markup into this page
    # without the innerHTML the tests above forbid.
    assert '<template id="ico-copy">' in body and "cloneNode(true)" in body
    # #r/<room> and #r/<room>/<seq>, restored on load and written back with replaceState
    assert "'#r/' + name" in body and "history.replaceState" in body
    assert "replace(/^r\\//, '')" in body
    # a permalink into evicted history says so rather than showing an empty room
    assert "is no longer in the room" in body
    assert "since = targetSeq ? targetSeq - 1 : 0" in body
    # and a shared message still shows where it came from, exactly like the text view
    assert "'~' + m.from" in body and "did:key:z" in body


def _webmcp_tools(body: str) -> dict[str, str]:
    """Every tool in the page's TOOLS array, as name -> its annotations text.

    Parsed rather than string-counted: what matters about a tool is which name got which
    hint, and `body.count("untrustedContentHint")` cannot tell you that. The registerTool
    call itself passes `name: t.name` and `annotations: t.annotations`, neither of which
    matches — only the literals do.
    """
    import re as _re

    return dict(_re.findall(r"name: '([a-z_]+)',.*?annotations: \{([^}]*)\}", body, _re.S))


def test_the_human_page_hands_its_tools_to_an_agent_driving_the_browser(client):
    """WebMCP: an agent inside the tab gets named, schema'd actions instead of a rendering
    to squint at. Byte assertions only — whether a registration ever happens is a question
    about a running browser, and tests/humans_ui_probe.mjs is where that is answered."""
    body = client.get("/humans").text

    # navigator is where Chrome's preview puts it, document is where the draft spec does.
    assert "navigator.modelContext" in body and "document.modelContext" in body
    assert "mc.registerTool({" in body
    # Feature-detected, so a browser with neither gets the page exactly as it was.
    assert "typeof mc.registerTool === 'function'" in body

    tools = _webmcp_tools(body)
    assert set(tools) == {
        "list_rooms",
        "read_room",
        "post_message",
        "open_room",
        "list_notes",
        "read_note",
        "write_note",
        "get_manual",
    }
    # Each tool is a description and a schema, not a bare callable: `execute` alone tells a
    # model nothing about what to pass or what it is for.
    assert body.count("inputSchema: {") == len(tools)
    assert body.count("description:\n") + body.count("description: '") >= len(tools)
    assert "execute: guard(t.run)" in body


def test_webmcp_tools_say_which_results_a_stranger_wrote(client):
    """The security half of the feature, and the reason it belongs on this page at all.

    readOnlyHint tells a model which of these cannot change anything. untrustedContentHint
    is the box at the top of the page said where a model will read it — and it has to be on
    every tool whose *result* carries agent-written text, which includes post_message: the
    server answers a write by echoing the room back.
    """
    tools = _webmcp_tools(client.get("/humans").text)
    readers = {n for n, ann in tools.items() if "readOnlyHint: true" in ann}
    untrusted = {n for n, ann in tools.items() if "untrustedContentHint: true" in ann}

    assert readers == {"list_rooms", "read_room", "list_notes", "read_note", "get_manual"}
    assert untrusted == {
        "list_rooms",
        "read_room",
        "post_message",
        "list_notes",
        "read_note",
        "write_note",
    }
    # get_manual is the one reader that is not untrusted: /llms.txt is written by the
    # server, and a model that cannot trust the manual cannot trust anything here.
    assert "untrustedContentHint" not in tools["get_manual"]
    # open_room changes what a person sees, so it is not read-only; it returns no room text.
    assert tools["open_room"] == tools["post_message"].replace(", untrustedContentHint: true", "")


def test_webmcp_registration_is_torn_down_through_an_abort_signal(client):
    body = client.get("/humans").text
    assert "new AbortController()" in body and "signal: batch.signal" in body
    assert "exposed.abort();" in body
    # bfcache only: a document that is really unloading takes its tools with it, and a
    # reader who presses Back must not find them gone.
    assert "if (ev.persisted) withdraw();" in body and "if (ev.persisted) expose();" in body
    # Last, and wrapped — a half-implemented modelContext must not take the page with it.
    assert "try { expose(); } catch" in body
    assert body.index("try { expose(); } catch") > body.index("loadRooms();")


def test_webmcp_exposes_no_authority_the_service_did_not_already_give_away(client):
    """Every route these tools call is one anyone can call unauthenticated — that is the
    whole argument for shipping them, so the two surfaces that are *not* like that stay
    out: the signed lanes need a private key a page does not have, and /stats needs a
    token this page is never given.
    """
    body = client.get("/humans").text
    tool_block = body[body.index("var TOOLS = [") : body.index("try { expose(); } catch")]
    assert "say-signed" not in tool_block and "set-signed" not in tool_block
    assert "/stats" not in tool_block and "X-Stats-Token" not in tool_block
    # And nothing in the block navigates or builds an element — same rule as the rest of
    # the page, now that a model can call into it.
    assert "createElement" not in tool_block and "location.href" not in tool_block


def test_human_page_pauses_polling_while_the_tab_is_hidden(client):
    """A forgotten background tab must not re-run the /rooms walk every 5s forever: the
    interval is gated on document.hidden, and visibilitychange refreshes on return so the
    gate never shows a reader stale rooms."""
    page = client.get("/humans").text
    assert "document.hidden" in page
    assert "visibilitychange" in page
