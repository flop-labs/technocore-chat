"""Property tests for the parse/adversarial surface of \x60store.py\x60.

Where \x60tests/test_store_stateful.py\x60 generates lifecycle *sequences* (the stateful half of
the adversarial surface), this file fuzzes the pure functions that face hostile input one
call at a time: the single-line sweep, the name allowlist, the JSONL line codec, and the
timestamp format. Each property is a Hypothesis @given test with derandomize=True, so CI
is deterministic and a failure is a change in store.py, not a new seed.

The serialization property reconstructs _write_record's line format exactly
(json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode()) rather than calling
the writer, because the writer takes the store lock, reaps and compacts — none of which is
what the round-trip claim is about.

Run: uv run --group dev python -m pytest tests/unit/test_parse_properties.py
"""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from hypothesis import assume, given, settings
from hypothesis import strategies as st

import didkey
import store

# Derandomized and deadline-free everywhere: the whole file runs in CI on every push, and
# a property suite whose failures cannot be reproduced is worse than no suite.
CI = settings(derandomize=True, deadline=None, max_examples=75)

# The full hostile alphabet — surrogates included, for real this time. codec="utf-8"
# alone EXCLUDES lone surrogates (they are not UTF-8-encodable), which once made this
# strategy silently narrower than the sweep's contract: category Cs was claimed but
# never generated (review: PR #57). So: encodable text, pure-surrogate runs, and a mix,
# because a hostile URL can land a lone surrogate in the middle of real text via
# percent-encoded CESU-8 and the sweep has to take each one out.
_SURROGATE = st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF)
ANY_TEXT = st.one_of(
    st.text(st.characters(codec="utf-8"), max_size=300),
    st.text(_SURROGATE, max_size=4),
    st.builds(
        lambda a, b: a + "\ud800" + b,
        st.text(st.characters(codec="utf-8"), max_size=64),
        st.text(st.characters(codec="utf-8"), max_size=64),
    ),
)


def _clean_or_none(text: str) -> str | None:
    """clean_text(text), or None when the sweep legitimately refused it (empty result).

    A refusal for emptiness is covered by its own assertion inside the sweep property, so
    callers here skip those inputs rather than assume-filtering them up front (which would
    spend the example budget on retries).
    """
    try:
        return store.clean_text(text)
    except store.StoreError:
        return None


def _json_line(rec: dict) -> bytes:
    """The exact bytes _write_record appends (minus the trailing newline, which
    reverse_lines strips before _parse sees the line)."""
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode()


# --------------------------------------------------------------------------- sweep


@given(ANY_TEXT)
@CI
def test_clean_text_sweeps_trims_and_is_idempotent(text: str) -> None:
    out = _clean_or_none(text)
    if out is None:
        # The only other refusal is the length cap, unreachable at <= 300 chars, so None
        # here means the sweep ate everything: assert that is what happened.
        swept = "".join(
            " "
            if (
                unicodedata.category(c) in store.INVISIBLE_CATEGORIES
                and c not in store.SWEEP_EXEMPT
            )
            else c
            for c in text
        )
        assert swept.strip() == ""
        return
    # No swept category survives except the two joiners SWEEP_EXEMPT holds out, and neither
    # end carries so much as a space.
    assert all(
        unicodedata.category(c) not in store.INVISIBLE_CATEGORIES or c in store.SWEEP_EXEMPT
        for c in out
    )
    assert out == out.strip()
    assert not out.startswith(" ")
    assert not out.endswith(" ")
    # The sweep is 1:1 per character and strip only removes: output never grows.
    assert len(out) <= len(text)
    # Idempotence: the output is already a fixed point of the sweep.
    assert store.clean_text(out) == out


# clean_text takes its limit as a parameter, so the cap can be exercised over-limit at
# any size — no need to generate 4 KiB strings (a large-base-example health check smell).
# Every filler char is visible and non-whitespace, so the sweep is the identity and the
# length check is the only refusal that can fire.
VISIBLE_CHARS = ("a", "0", "-", "中", "🜂", "é")


@given(
    limit=st.integers(min_value=1, max_value=64),
    extra=st.integers(min_value=1, max_value=32),
    filler=st.sampled_from(VISIBLE_CHARS),
)
@settings(derandomize=True, deadline=None, max_examples=50)
def test_clean_text_over_limit_raises_store_error(limit: int, extra: int, filler: str) -> None:
    text = filler * (limit + extra)
    # app.py maps StoreError to HTTP 400, so "raises StoreError with the too-long
    # message" is the real contract.
    try:
        out = store.clean_text(text, limit=limit)
    except store.StoreError as exc:
        assert str(exc).startswith("text too long")
        return
    raise AssertionError(f"over-limit text accepted: {len(out)} chars, no StoreError")


# ------------------------------------------------------------------------ names


@given(st.text(st.characters(codec="utf-8"), max_size=64))
@CI
def test_valid_name_accepts_only_safe_path_segments(name: str) -> None:
    try:
        out = store.valid_name(name)
    except store.StoreError:
        return  # rejection is the next property's job
    # Accepted => matches the allowlist exactly, is its own final path segment (no
    # traversal, no separator, no ".." anywhere), and cannot read as an option.
    assert store.NAME_RE.fullmatch(out) is not None
    assert Path(out).name == out
    assert "/" not in out
    assert ".." not in out
    assert not out.startswith("-")


# Names NAME_RE would accept, mutated into every traversal/denial shape a caller can
# produce. ..%2F arrives here already decoded (that is app.py's job) — i.e. as ".." — so
# the store-level rule to hold is exactly the segment rule above; the raw form is kept as
# a constant case because it must be rejected either way.
GOOD_NAMES = st.from_regex(r"[a-z0-9][a-z0-9_-]{0,47}", fullmatch=True)

BAD_NAMES = st.one_of(
    st.just(".."),
    st.just("."),
    st.just("../"),
    st.just("..%2F"),
    st.builds("../{}".format, GOOD_NAMES),
    st.builds("{}/../{}".format, GOOD_NAMES, GOOD_NAMES),
    st.builds("{}\\..".format, GOOD_NAMES),  # backslash traversal
    GOOD_NAMES.map(lambda n: "-" + n),  # reads as an option to a shell
    # Only names that actually change: GOOD_NAMES admits digit-only names like "0",
    # whose upper() is a no-op and therefore still VALID — an unfiltered map let those
    # reach the must-be-rejected assertion below (review: PR #57).
    GOOD_NAMES.filter(lambda n: n != n.upper()).map(str.upper),
    GOOD_NAMES.map(lambda n: n + "\n"),
    GOOD_NAMES.map(lambda n: n + "x" * 48),  # past the 48-char ceiling
    st.just(""),
)


@given(BAD_NAMES)
@CI
def test_valid_name_rejects_traversal_and_denial_shapes(name: str) -> None:
    try:
        store.valid_name(name)
    except store.StoreError:
        return
    raise AssertionError(f"traversal-shaped name accepted: {name!r}")


# --------------------------------------------------------------------- line codec

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _did(seed: int) -> str:
    """A real did:key:z6Mk... from arbitrary bytes — the signed lane's actual alphabet."""
    raw = seed.to_bytes((seed.bit_length() + 7) // 8 or 1, "big")[-32:].rjust(32, b"\0")
    # b58 of the multicodec prefix naturally starts "6Mk", so the multibase tag "z" is
    # the only thing to prepend; '1' is base58 zero, so rjust pads safely.
    return "did:key:z" + _b58(b"\xed\x01" + raw).rjust(47, "1")


def _b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, d = divmod(n, 58)
        out = _B58[d] + out
    return out


@given(
    raw=ANY_TEXT,
    seq=st.integers(min_value=1, max_value=2**53),
    nick=GOOD_NAMES,
    did_seed=st.integers(min_value=1, max_value=2**255),
    nonce=st.integers(min_value=0, max_value=2**63),
    second_precision=st.booleans(),
    signed=st.booleans(),
)
@CI
def test_write_record_line_parses_back_to_same_fields(
    raw: str,
    seq: int,
    nick: str,
    did_seed: int,
    nonce: int,
    second_precision: bool,
    signed: bool,
) -> None:
    text = _clean_or_none(raw)
    if text is None:
        assume(False)  # empty-after-sweep inputs have no record to round-trip
    # Both ts shapes coexist on disk (pre-microsecond records), so both round-trip.
    ts = store._now()
    if second_precision:
        ts = ts[:19] + "Z"
    if signed:
        did = _did(did_seed)
        assert didkey.is_did(did)  # the alphabet claim: keys we would actually verify
        rec = {"seq": seq, "ts": ts, "from": did, "text": text, "nonce": nonce}
    else:
        rec = {"seq": seq, "ts": ts, "from": store.valid_name(nick), "text": text}
    assert store._parse(_json_line(rec)) == rec


# Torn writes, NUL bytes, CJK/emoji payloads, non-UTF8 garbage: whatever hits a room
# file, _parse answers dict-or-None and never raises — the reader loop depends on that.
HOSTILE_BYTES = st.one_of(
    st.binary(max_size=256),
    st.builds(lambda s: b'{"seq":1,"text":"' + s.encode(), st.text(st.characters(), max_size=64)),
    st.builds(lambda n: b'{"seq":' + str(n).encode(), st.integers(0, 2**63)),
)


@given(HOSTILE_BYTES)
@settings(derandomize=True, deadline=None, max_examples=100)
def test_parse_never_raises_on_arbitrary_bytes(line: bytes) -> None:
    out = store._parse(line)
    assert out is None or (isinstance(out, dict) and isinstance(out.get("seq"), int))


# --------------------------------------------------------------------- timestamps


@given(st.datetimes(min_value=datetime(1000, 1, 1)), st.sampled_from((0, 6)))
@settings(derandomize=True, deadline=None, max_examples=50)
def test_timestamp_shapes_parse_with_fromisoformat(dt: datetime, frac_digits: int) -> None:
    # _now() writes 6 fractional digits; records from before that change carry 0. Both
    # must parse for any reader that treats ts as a timestamp rather than opaque.
    # Years >= 1000 because that is the store's real domain — _now() is datetime.now(),
    # always four digits — and below it the property stops testing ts parsing and starts
    # testing the platform: glibc strftime('%Y') emits unpadded '999', which
    # fromisoformat rejects, while macOS emits '0999' and it parses. That split is
    # real (it failed CI on Linux after passing three local macOS runs) but it is a
    # libc fact, not a store contract, so the domain excludes it on purpose.
    ts = dt.strftime("%Y-%m-%dT%H:%M:%S" + (".%f" if frac_digits else "") + "Z")
    want = dt.replace(tzinfo=UTC)
    if not frac_digits:  # second precision: the microseconds never made it to the string
        want = want.replace(microsecond=0)
    assert datetime.fromisoformat(ts) == want
    # And the live format, off the real clock.
    assert datetime.fromisoformat(store._now()) is not None
