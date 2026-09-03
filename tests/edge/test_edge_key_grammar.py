"""Run: uv run --group dev python -m pytest tests

The revalidating lane keys /rooms on a cache key rather than on the raw URL, and that key is built
in JavaScript while the reply it names is built in Python. Both read the same `limit`, with
different parsers: `cacheKey` admits a bare run of ASCII digits, the handler calls `int()`, and
`int()` is far wider — 680 code points on this interpreter, plus signs, underscores and surrounding
whitespace. cacheKey says why the difference matters, beside the guard that closes it: "a
disagreement there would not be a miss but a wrong answer — one caller's row count served to
another."

Nothing enumerated it. There is no JS harness in this repo, so the grammar is read out of worker.js
and measured against the running origin, in three parts: the guard refuses a form rather than
coercing it, the two parsers really do differ — by 670 code points and by every form Python reads
around a number — and inside the grammar the key names the reply the origin gives.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import unicodedata
import urllib.parse

import _client
import pytest

import app

EDGE = pathlib.Path(__file__).resolve().parents[2] / "edge"

client = _client.client  # the shared TestClient fixture

ASCII_DIGITS = set("0123456789")

# A `_cursor` fallback no form below can produce, so "the origin read a number out of this" and
# "the origin gave up and used its default" can never be mistaken for one another.
NO_NUMBER = -1


def _snapshot_module():
    spec = importlib.util.spec_from_file_location("edge_snapshot", EDGE / "snapshot.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cache_key_source() -> str:
    """cacheKey's own source, for the assertions there is no JS harness to make properly."""
    worker = (EDGE / "src" / "worker.js").read_text(encoding="utf-8")
    body = worker[worker.index("function cacheKey(") :]
    return body[: body.index("\n}\n")]


def _digit_grammar() -> re.Pattern[str]:
    """The clamped-parameter grammar, compiled out of worker.js rather than restated here.

    A copy would keep passing after someone widened the Worker's own pattern, which is the drift
    worth catching — the same reason the signer tests match the server's SIG_PATTERN instead of a
    local copy of it.

    Translated as `fullmatch` on the bare class, never `match` on an anchored one: Python's `$`
    also matches immediately before a trailing newline where JavaScript's does not, so
    `^[0-9]{1,9}$` would accept "5\\n" here and reject it there — and "5\\n" is one of the forms
    below, which the origin reads as five.
    """
    found = re.search(r"/\^\[0-9\]\{(\d+),(\d+)\}\$/\.test\(", _cache_key_source())
    assert found, "cacheKey must gate a clamped parameter on a bare ASCII digit-run pattern"
    return re.compile(rf"[0-9]{{{found.group(1)},{found.group(2)}}}")


def test_a_limit_outside_the_grammar_gets_no_key_rather_than_a_coerced_one():
    """A source assertion, deliberately, and narrow — there is no JS harness in this repo.

    Everything below rests on this one property: a `limit` the grammar refuses yields no key at
    all, so that caller is answered by the origin alone and no shared copy is read or written. The
    refusal is what makes the width difference harmless rather than dangerous.

    The edit it guards against is the reasonable-looking one — coercing an odd form to its nearest
    digit string instead of giving up. That would file a reply built for one number under a key
    naming a different one, which is the failure the comment beside the guard describes.
    """
    body = _cache_key_source()

    guards = [line for line in body.splitlines() if ".test(raw)" in line]
    assert len(guards) == 1, f"expected one grammar test in cacheKey, found {len(guards)}"
    guard = guards[0]
    assert guard.strip().startswith("if (!"), f"the grammar must gate, not decorate: {guard!r}"
    assert guard.strip().endswith("return null;"), (
        f"a refused limit must abandon the key rather than fall through to one: {guard.strip()!r}"
    )
    assert body.index(".test(raw)") < body.index("keep.set(name, String("), (
        "a clamped value must clear the grammar before it can enter the key"
    )
    assert "url.searchParams.get(name) === wanted" in body, (
        "a matched parameter must survive on exact equality — a looser comparison would let the "
        "edge key one rendering while the origin serves another"
    )


def test_a_present_but_empty_limit_gets_no_key_either(client):
    """The other way a form falls outside the grammar: `?limit=` present, with nothing after it.

    A different failure from the table below, which is why it is its own rung. Those forms are each a
    *specific* number to the origin, so admitting one would misname a page. This one is no number at
    all — the origin falls back to its own default, a page far larger than the floor the Worker's
    `Number(raw) || rule.min` would spell it as. Admitting it would file the default reply under a
    name meaning a single row.

    The rung above cannot see this one. An empty value is not a code point, so a walk over the code
    space never offers it, and a quantifier lowered to `{0,9}` still reads as a bounded digit run to
    the pattern the grammar is extracted with — measured: that edit passes every other test here.
    """
    limit = _snapshot_module().rooms_key()["/rooms"]["clamped"]["limit"]
    assert not _digit_grammar().fullmatch(""), (
        "an empty limit is admitted to the key, so the origin's default page would be stored under "
        f"the name limit={limit['min']}"
    )

    for i in range(8):
        client.get(f"/r/void{i}/say/bot/hi")

    def names(query: str) -> list[str]:
        payload = client.get(f"/rooms?format=json{query}").json()
        return [row["room"] for row in payload["rooms"]]

    assert app._cursor("", NO_NUMBER) == NO_NUMBER, "an empty limit is no number to the origin"
    assert names("&limit=") == names(""), "an empty limit must read as the origin's own default"
    assert len(names("&limit=")) > limit["min"], (
        "the default page has to be larger than the key's floor, or this asserts nothing"
    )


def test_no_code_point_but_the_ten_ascii_digits_can_key_a_shared_copy():
    """The safety property, stated completely rather than by example: across the whole code space
    the grammar admits exactly the ten ASCII digits as a single character. Every digit of every
    other script, and every look-alike, gets no key — whatever the origin goes on to make of it.

    Exhaustive on purpose. Which characters a parser accepts is a fact about tables outside this
    repo, so a handful of examples can stop covering the gap with no commit here at all.
    """
    grammar = _digit_grammar()
    admitted = {chr(cp) for cp in range(0x110000) if grammar.fullmatch(chr(cp))}
    assert admitted == ASCII_DIGITS, (
        f"the key grammar admits {sorted(admitted - ASCII_DIGITS)!r} beyond the ASCII digits, so "
        f"the origin and the edge no longer agree on what a limit is"
    )


def test_the_origin_reads_a_number_from_far_more_code_points_than_the_key_admits():
    """Why the rung above is load-bearing and not a tautology: the parser on the other side of the
    lane accepts sixty-eight times as many characters. `int()` takes every decimal digit Unicode
    defines — 680 code points on unicodedata 15.0.0, measured — and reads each as its numeric
    value, so 670 of them reach the handler as a number the key has no spelling for.

    Pinned separately from the grammar because it is a property of the Unicode tables rather than
    of this repo: a later table can widen the origin's side with no commit here, and this is the
    rung that notices.
    """
    grammar = _digit_grammar()
    decimals = [chr(cp) for cp in range(0x110000) if unicodedata.category(chr(cp)) == "Nd"]
    assert len(decimals) > 600, f"only {len(decimals)} decimal digits found; the walk is wrong"

    misread = [c for c in decimals if int(c) != unicodedata.decimal(c)]
    assert not misread, f"int() and the Unicode tables disagree about {misread!r}"

    outside = [c for c in decimals if not grammar.fullmatch(c)]
    assert len(outside) == len(decimals) - len(ASCII_DIGITS), (
        "every decimal digit but the ASCII ten must fall outside the grammar"
    )


@pytest.mark.parametrize(
    ("script", "digit"),
    [
        ("arabic-indic", "٥"),
        ("devanagari", "५"),
        ("fullwidth", "５"),
        ("nko", "߅"),
        ("osmanya", "\U000104a5"),
    ],
)
def test_a_non_ascii_digit_limit_is_a_live_number_to_the_origin(client, script, digit):
    """The gap is reachable over HTTP rather than only inside the parser: each of these is the
    digit five to the handler, and the caller gets a five-room page.

    Distinct from the junk limits covered elsewhere, and the distinction is the whole point. `²` is
    category No, `int()` raises on it, and the handler falls back to its default — nonsense in,
    default page out. These are category Nd: `int()` succeeds, so the caller gets a page of exactly
    the size they asked for, spelled in a way the cache key cannot hold.

    Percent-encoded because the form has to survive a request line, which is also why the forms
    that cannot are exercised against the parser directly rather than over HTTP.
    """
    for i in range(8):
        client.get(f"/r/gram{i}/say/bot/hi")

    def names(query: str) -> list[str]:
        payload = client.get(f"/rooms?format=json&{query}").json()
        return [row["room"] for row in payload["rooms"]]

    assert len(names("")) > 5, "there must be more rooms than the limit, or it caps nothing"

    asked = f"limit={urllib.parse.quote(digit, safe='')}"
    assert len(names(asked)) == 5, f"{script} five was not read as five"
    assert names(asked) == names("limit=5"), f"{script} five named a different page from ASCII 5"


@pytest.mark.parametrize(
    ("form", "raw", "reads"),
    [
        ("a leading plus", "+5", 5),
        ("an underscore separator", "1_0", 10),
        ("leading whitespace", " 5", 5),
        ("trailing whitespace", "5 ", 5),
        ("a trailing newline", "5\n", 5),
        ("a non-breaking space", "\xa05", 5),
        ("a tab", "\t5", 5),
        ("a non-ASCII digit", "٥", 5),
        ("two non-ASCII digits", "١٥", 15),
        ("ASCII and non-ASCII mixed", "1٥", 15),
        ("ten digits", "1234567890", 1234567890),
        ("nine digits behind a zero", "0000000009", 9),
    ],
)
def test_the_key_refuses_the_forms_python_reads_around_a_number(form, raw, reads):
    """The four widenings cacheKey's comment names — underscores, signs, whitespace, non-ASCII
    digits — plus the two the length bound catches, enumerated against the origin's real parser.

    Each of these is a *specific* number to the handler and not its default, which is what makes
    the row worth having: were any of them admitted, the key would have to spell a number that the
    grammar has no way to write, and the entry would answer a question nobody asked. Asserted
    against `_cursor` rather than over HTTP because several cannot travel in a request line at all
    — a raw newline is refused before the app sees it — and reachability is covered above.
    """
    assert app._cursor(raw, NO_NUMBER) == reads, f"{form} is not read as {reads} by the origin"
    assert reads != NO_NUMBER, f"{form} would only prove the origin gave up, not that it read"
    assert not _digit_grammar().fullmatch(raw), (
        f"{form} ({raw!r}) is admitted to the cache key, so the edge must spell {reads} — which "
        f"it cannot do from this form"
    )


@pytest.mark.parametrize(
    ("form", "raw"),
    [
        ("three characters", "007"),
        ("seven characters", "0000005"),
        ("the full width allowed", "000000003"),
        ("the full width, all zeros", "000000000"),
    ],
)
def test_a_zero_padded_limit_names_the_same_reply_its_clamped_key_does(client, form, raw):
    """Inside the grammar the two parsers have to agree, and the padding is where they might not:
    a leading zero has meant octal in enough languages to be worth pinning rather than assuming.
    Both read these as plain decimal, and all-zeros lands on the same floor on both sides — the
    Worker's `Number(raw) || rule.min` against the origin's, which it applies twice over: once where
    the query is parsed and again where the rows are sliced. Asserted as the reply rather than as
    either of those lines, because removing one of them alone changes nothing a caller can see.

    That agreement is the reason the grammar is a bare digit run and nothing wider: on a run of
    ASCII digits there is no parser left to disagree with.

    Kept below the room count on purpose. A limit at or past the total returns everything, so it
    would match its clamped form even if the clamp were wrong.
    """
    limit = _snapshot_module().rooms_key()["/rooms"]["clamped"]["limit"]
    clamp = [line for line in _cache_key_source().splitlines() if "keep.set(name, String(" in line]
    assert len(clamp) == 1 and "Math.min(Number(raw) || rule.min, rule.max)" in clamp[0], (
        f"the arithmetic mirrored below is only faithful while the Worker clamps that way: {clamp!r}"
    )

    for i in range(8):
        client.get(f"/r/pad{i}/say/bot/hi")

    def names(query: str) -> list[str]:
        payload = client.get(f"/rooms?format=json&{query}").json()
        return [row["room"] for row in payload["rooms"]]

    # The Worker's arithmetic, mirrored: String(Math.min(Number(raw) || rule.min, rule.max)).
    key = str(min(int(raw) or limit["min"], limit["max"]))
    assert key.isdigit() and _digit_grammar().fullmatch(raw), f"{form} is not a keyable form"
    assert len(names(f"limit={raw}")) == int(key), f"{form} did not cap at {key}"
    assert names(f"limit={raw}") == names(f"limit={key}"), (
        f"{form} names a different page from the key it would be stored under"
    )


@pytest.mark.parametrize(
    "raw", ["xml", "JSON", "Json", "jsonn", "json2", "", " json", "json ", "text", "0", "true"]
)
def test_a_format_the_key_drops_is_the_rendering_the_origin_gives_without_it(client, raw):
    """The other half of the spec. `format` survives into the key only when it equals "json"
    exactly — a strict comparison, so a different case or a trailing space is a different value —
    and every value that does not survive has to be the same rendering as no `format` at all.
    Otherwise one of them would be answered out of the entry belonging to the other.

    Compared byte for byte, which the empty listing is what makes safe: a populated one carries
    per-room ages that move between two requests, while with no rooms yet there is nothing in
    either rendering a clock can change. The fixture is left empty on purpose.
    """
    plain = client.get("/rooms")
    as_json = client.get("/rooms?format=json")
    assert plain.text != as_json.text, "the two renderings must differ, or this asserts nothing"
    assert "rooms" in as_json.json(), "the json rendering is the one that carries a payload"

    dropped = client.get(f"/rooms?format={urllib.parse.quote(raw, safe='')}")
    assert dropped.status_code == 200
    assert dropped.headers["content-type"] == plain.headers["content-type"], (
        f"format={raw!r} is dropped from the key but changes the rendering at the origin"
    )
    assert dropped.text == plain.text, (
        f"format={raw!r} is dropped from the key, so it must be the reply that key already names"
    )
