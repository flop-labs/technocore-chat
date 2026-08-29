"""Run: uv run --group dev python -m pytest tests

The conformance vectors, checked against the implementation that generated them.

The vectors exist for clients in other languages — three are now on npm — but a vector file
nobody re-derives is a stale vector file, and a client that trusts a stale one is worse off
than a client with none. So these tests point in the opposite direction from
`generate_vectors.py`: it reads the implementation and writes the file, and this reads the
file and checks the implementation. Between them the file cannot drift from the server
without something here going red.

`test_vectors_are_not_stale` is the one that matters most: it regenerates in memory and
compares, so a change to `clean_text`'s category list or to `didkey`'s accepted shapes fails
HERE, on the PR that makes it, with a diff — rather than silently in someone else's client
weeks later, as an unexplained 403.

What is deliberately NOT asserted: that another implementation agrees. That cannot be
checked from inside this repo, and pretending otherwise would make these tests a claim about
software they cannot see. `tests/conformance/runner.mjs` is where an implementation gets
checked, and it is run by whoever owns that implementation.

`verify.mjs` is the one exception, and only because it is not that claim: it re-verifies THIS
directory's own signatures from Node's crypto rather than checking anybody's client, so
running it here says "the vectors are real Ed25519 signatures" instead of "your client is
right". It is skipped when no `node` binary exists, because CI is pure-Python.
"""

import base64
import importlib.util
import json
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

import didkey
import store

HERE = Path(__file__).parent
VECTORS = json.loads((HERE / "vectors.json").read_text(encoding="utf-8"))

# The sweep is `unicodedata.category(c) in (six categories)`, so its answers come from the
# interpreter's Unicode tables, not from this repo. CI pins 3.12 (.python-version) and this
# repo's three interpreters disagree — 3.12 is Unicode 15.0, 3.13 is 15.1, 3.14 is 16.0 — so
# "does the implementation match the vectors" is only a well-formed question on the version
# the vectors were built with. Rather than fail everywhere else, the cases that can actually
# move are marked `version_sensitive` and skipped off-version; everything else always runs.
SAME_UNICODE = unicodedata.unidata_version == VECTORS["provenance"]["unicode_version"]
OFF_VERSION = (
    f"built under Unicode {VECTORS['provenance']['unicode_version']}, running "
    f"{unicodedata.unidata_version} — regenerate under CI's interpreter (.python-version) "
    f"to check this"
)


def _generator():
    """Load the generator by path. It is a script, not a package — no __init__.py, and
    `tests` being on sys.path does not make `conformance.generate_vectors` importable.

    The two asserts are for the type checker as much as the reader: `spec_from_file_location`
    and `spec.loader` are both Optional, and `ty check` is part of CI.
    """
    spec = importlib.util.spec_from_file_location("_genvec", HERE / "generate_vectors.py")
    assert spec is not None and spec.loader is not None, "generate_vectors.py is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text(code_points):
    """Rebuild a case's text from code points. The vectors carry integers rather than JSON
    strings because the swept set includes Cs, and a lone surrogate has no UTF-8 encoding —
    `"a\\ud800b"` survives in the file only as an escape, and a consumer that re-encodes it to
    UTF-8 or folds it to U+FFFD would test a different character."""
    return "".join(chr(c) for c in code_points)


# ------------------------------------------------------------------------------- the sweep


@pytest.mark.parametrize("case", VECTORS["sweep_cases"], ids=lambda c: c["name"])
def test_sweep_matches_the_vector(case):
    """`store.clean_text` still does what the vector says it does."""
    if case["version_sensitive"] and not SAME_UNICODE:
        pytest.skip(OFF_VERSION)
    text = _text(case["in_cp"])
    if case["raises_empty"]:
        # The sweep ate everything. clean_text refuses rather than storing a blank record,
        # and says which characters it swept — a caller that got a bare "empty text" would
        # re-send the same bytes and get the same refusal.
        with pytest.raises(store.StoreError, match="nothing visible was left"):
            store.clean_text(text)
        return
    assert store.clean_text(text) == _text(case["out_cp"])


def test_the_sweep_is_idempotent():
    """Sweeping swept text changes nothing.

    This is what makes the whole signed lane work, and it is load-bearing rather than
    incidental: a client sends the text it signed, the server sweeps what it receives, and
    the signature is checked against that. If sweeping twice differed from sweeping once,
    a *conformant* client would 403 — the server would verify against text the client never
    saw. It also fixes the failure mode for a NON-conformant client as a refusal rather than
    a bypass: text that reaches a reader has been through the sweep, whatever the client did.
    """
    for case in VECTORS["sweep_cases"]:
        if case["raises_empty"]:
            continue
        once = store.clean_text(_text(case["in_cp"]))
        assert store.clean_text(once) == once, case["name"]


def test_the_swept_categories_are_still_the_six_in_the_vectors():
    assert list(store.INVISIBLE_CATEGORIES) == VECTORS["provenance"]["invisible_categories"]
    assert store.MAX_TEXT_CHARS == VECTORS["provenance"]["max_text_chars"]


def test_zs_is_not_swept():
    """U+00A0 is Zs, and Zs is not one of the six. An interior no-break space survives.

    Called out separately because "replace the invisible characters" reads like "replace the
    whitespace", and a client that sweeps `\\s` or `Unicode.isWhitespace` signs different
    bytes for any text containing one. At the ends it disappears anyway — `str.strip()`
    strips it — which is what makes the interior case the one worth pinning.
    """
    assert store.clean_text("a b") == "a b"
    assert store.clean_text(" hi ") == "hi"


# -------------------------------------------------------------------------------- did:key


@pytest.mark.parametrize("identity", VECTORS["identities"], ids=lambda i: i["fingerprint"])
def test_did_and_fingerprint_round_trip(identity):
    gen = _generator()
    public = didkey.public_key(identity["did"])
    assert len(public) == 32
    assert gen.did_for(public) == identity["did"]
    assert gen.fingerprint(identity["did"]) == identity["fingerprint"]
    fp = identity["fingerprint"]
    assert identity["sharded_write_path"] == f"/kv/did-{fp[:2]}/{fp[2:]}"
    assert identity["legacy_read_path"] == f"/kv/did/{fp}"


@pytest.mark.parametrize("case", VECTORS["did_invalid"], ids=lambda c: c["why"])
def test_malformed_dids_are_refused(case):
    """Fails closed, with the same message the vector recorded — a client matching on the
    error text is relying on it, and these are the only diagnosis a 400 carries."""
    with pytest.raises(didkey.DidError) as exc:
        didkey.public_key(case["did"])
    assert str(exc.value) == case["error"]


# ------------------------------------------------------------------- signatures and payload


@pytest.mark.parametrize("case", VECTORS["signature_cases"], ids=lambda c: c["name"])
def test_canonical_payload_and_signature(case):
    """The payload is `<room>|<nonce>|<swept text>`, UTF-8, and the vector's signature
    verifies over exactly those bytes.

    `seq` and `ts` are outside the payload on purpose: both are the server's, assigned after
    verification, so a signature could not cover them without the client predicting them.
    """
    swept = store.clean_text(_text(case["text_raw_cp"]))
    assert swept == _text(case["text_swept_cp"])
    payload = f"{case['room']}|{case['nonce']}|{swept}"
    assert payload.encode("utf-8").hex() == case["payload_utf8_hex"]
    didkey.verify(case["did"], case["sig_canonical"], payload)


@pytest.mark.parametrize("case", VECTORS["signature_cases"], ids=lambda c: c["name"])
def test_only_the_canonical_spelling_of_a_signature_is_accepted(case):
    """One signature, sixteen strings, one of them accepted.

    64 raw bytes is 86 unpadded base64url characters — 516 bits of alphabet for 512 bits of
    signature — so the final character's low 4 bits carry no signature and sixteen distinct
    strings decode to the same bytes. Ed25519 cannot tell them apart, because it never sees
    the encoding: it is handed the decoded bytes and they are identical. So the refusal
    cannot come from the crypto, and #178 put it in `SIG_PATTERN` instead, pinning the last
    character to the four values whose low bits are zero.

    Both halves are asserted here because they are different claims and only one of them
    moved. The bytes are still identical — that is base64, and it is *why* the pattern has to
    do this work. What changed is that fifteen of the sixteen are now refused on the encoding
    before any verification runs.

    The direction worth a client author's attention is the second one. Every base64url
    decoder in circulation ignores the slack bits, so a client that decodes a signature and
    re-encodes it gets bytes that verify and a string the server refuses — a 403 with nothing
    wrong in it. Emit what `base64.urlsafe_b64encode` or `Buffer.toString("base64url")`
    produce and this cannot happen; hand-roll the encoder and it can.
    """
    payload = bytes.fromhex(case["payload_utf8_hex"]).decode("utf-8")
    spellings = case["sig_same_bytes_spellings"]
    assert len(spellings) == 16
    assert len(set(spellings)) == 16
    raw = {base64.urlsafe_b64decode(s + "==") for s in spellings}
    assert len(raw) == 1, "all sixteen must decode to the same 64 bytes"

    canonical, others = spellings[0], spellings[1:]
    assert canonical == case["sig_canonical"], "canonical must be recorded first"
    didkey.verify(case["did"], canonical, payload)
    for spelling in others:
        with pytest.raises(didkey.DidError, match="signature encoding"):
            didkey.verify(case["did"], spelling, payload)


@pytest.mark.parametrize("case", VECTORS["signature_cases"], ids=lambda c: c["name"])
def test_this_repos_encoder_only_ever_emits_the_canonical_spelling(case):
    """Sixteen spellings exist; exactly one is ever PRODUCED — which is why #178 was safe.

    This is the assertion that made the tightening in #178 a tightening rather than a
    compatibility break, and it is the one to keep pointing at now that #178 has landed
    (`SIG_PATTERN` = 85 free characters + `[AQgw]`, `src/didkey.py`). Narrowing an accepted
    set is only safe if nothing in circulation was relying on the part removed, and the way to
    know that is not to reason about it: the unused trailing bits are zero-filled by
    `base64.urlsafe_b64encode` and by Node's `Buffer.toString("base64url")`, so every
    signature either encoder has ever produced already ended in `AQgw`.

    Kept after the flip rather than folded into the test above, because it checks the opposite
    direction. That test asserts the server refuses the fifteen; this one asserts nobody was
    emitting them, and it is the second claim that answers "did anything break". If a future
    change makes the encoder emit a non-canonical spelling, this fails here rather than as a
    403 in a client that did everything right.
    """
    canonical = case["sig_canonical"]
    allowed = VECTORS["provenance"]["canonical_sig_last_chars"]
    assert canonical[-1] in allowed, f"{canonical[-1]!r} not in {allowed!r}"
    assert allowed == "".join(sorted(set(allowed))), "the canonical set is a sorted, unique set"
    # The published pattern and the recorded set have to be the same statement. Two copies of
    # a constraint drift, and this one is drifting in `/openapi.json` where clients read it.
    # `test_vectors_are_not_stale` would also catch this by regenerating — but only on the
    # Unicode version the file was built under, and it skips everywhere else. This does not,
    # so a widened SIG_PATTERN is caught on 3.13 and 3.14 too.
    assert didkey.SIG_RE.pattern.endswith(f"[{allowed}]"), (
        f"vectors record {allowed!r} as canonical but SIG_PATTERN is {didkey.SIG_PATTERN!r}"
    )
    assert case["sig_same_bytes_spellings"][0] == canonical, "canonical must be recorded first"
    others = case["sig_same_bytes_spellings"][1:]
    assert len(others) == 15
    assert canonical not in others


# ------------------------------------------------------------------- it is a fixture, not a key


def test_the_fixture_warning_travels_inside_the_file():
    """The seeds are public, and the warning saying so has to be in the JSON.

    A fixture gets copied far more often than it gets read, and a README does not travel with
    the bytes. Every `seed_hex` here is a counting pattern, so each derived `did:key` is
    controlled by everyone who can read the file — which makes "do not use these as an
    identity" a property of the data rather than documentation about it.

    Pinned rather than trusted because the failure is silent: a client seeded from this file
    works perfectly, right up until someone else signs with the same key.
    """
    assert VECTORS["test_only"] is True
    warning = VECTORS["warning"]
    assert "seed_hex" in warning
    assert "sign.py keygen" in warning, "the warning must say what to do instead"

    seeds = [i["seed_hex"] for i in VECTORS["identities"]]
    seeds += [c["seed_hex"] for c in VECTORS["signature_cases"]]
    for seed in seeds:
        assert len(seed) == 64
        assert len({seed[i : i + 2] for i in range(0, 64, 2)}) == 1, (
            f"{seed} is not obviously a fixture — a seed that looks random invites reuse, "
            f"which is the thing `warning` is trying to prevent"
        )


# ----------------------------------------------------------------- a second language's crypto


def test_node_reverifies_every_signature():
    """Check the signatures with Node's crypto instead of this repo's.

    Not a claim about anyone's client — see the module docstring. `verify.mjs` reimplements no
    protocol rule: it derives each key from its `did:key` and verifies the recorded signature
    over `payload_utf8_hex`. Without it, every signature in this directory is checked only by
    the library that produced it, which is homework marked by its own author.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node binary; verify.mjs is for whoever has one (CI is pure-Python)")
    script = HERE / "verify.mjs"
    assert script.exists()
    done = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, f"verify.mjs failed:\n{done.stdout}\n{done.stderr}"


@pytest.mark.parametrize("case", VECTORS["signature_cases"], ids=lambda c: c["name"])
def test_signing_the_raw_text_instead_of_the_swept_text_is_refused(case):
    """The mistake the vectors exist to catch, as an assertion.

    Every failing client so far fails this way: it signs what the caller passed and sends the
    swept form, or signs the swept form and forgets the trim. Both produce a signature over
    bytes the server does not compute, and the answer is a bare 403 that names neither cause.
    """
    raw = _text(case["text_raw_cp"])
    swept = _text(case["text_swept_cp"])
    if raw == swept:
        pytest.skip("this case is unchanged by the sweep, so there is no wrong payload")
    wrong = f"{case['room']}|{case['nonce']}|{raw}"
    with pytest.raises(didkey.SignatureError):
        didkey.verify(case["did"], case["sig_canonical"], wrong)


# ------------------------------------------------------------------------------- anti-drift


def test_the_unicode_tables_match_the_ones_the_vectors_were_built_with():
    """Not a failure off-version — a stated fact, so a surprising skip elsewhere has an
    explanation sitting next to it in the same output.

    U+180E is in the vectors precisely because a character CAN move: it was Zs before
    Unicode 6.3 and Cf after. That is a fact about the character, not a bug in anything, and
    the only defence is recording which tables produced the file.
    """
    if not SAME_UNICODE:
        pytest.skip(OFF_VERSION)
    assert unicodedata.unidata_version == VECTORS["provenance"]["unicode_version"]


def test_vectors_are_not_stale():
    """Regenerate in memory and compare.

    The point of the whole directory. Move the boundary — a category added to the sweep, a
    DID shape newly accepted, the payload separator changed — and this fails on the PR that
    moves it, with a diff, instead of turning into a 403 in a client this repo cannot see.

    The comparison is whole-file, which is only safe because `build()` records nothing
    environmental beyond the Unicode version: no timestamp, no Python version, no hostname.
    A file that changes when nothing changed is a file whose diffs stop being read. It is
    also why this can only run on-version — off-version the file SHOULD differ, and calling
    that staleness would be wrong.

    If it fails and the change was intended:  python tests/conformance/generate_vectors.py
    """
    if not SAME_UNICODE:
        pytest.skip(OFF_VERSION)
    fresh = _generator().build()
    if fresh != VECTORS:
        differing = [k for k in fresh if fresh[k] != VECTORS.get(k)]
        pytest.fail(
            "vectors.json no longer matches the implementation; regenerate it with\n"
            "    python tests/conformance/generate_vectors.py\n"
            f"sections that differ: {differing}"
        )


def test_the_generator_refuses_to_write_vectors_it_could_not_verify():
    """`_assert_sweep_matches` is a gate, not a note in the file.

    An unverified vector file is worse than no vector file: a client that trusts one has no
    way to discover it was wrong, and the symptom — a 403 — points at the client. So the
    generator checks its own replica of the sweep against `store.clean_text` before writing,
    and this pins that the check is real rather than vacuous.
    """
    gen = _generator()
    gen._assert_sweep_matches()  # must not raise here, where store imports

    original = gen.INVISIBLE_CATEGORIES
    try:
        gen.INVISIBLE_CATEGORIES = ("Cc",)  # a replica that no longer matches the server
        with pytest.raises(AssertionError):
            gen._assert_sweep_matches()
    finally:
        gen.INVISIBLE_CATEGORIES = original
