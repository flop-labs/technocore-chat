"""Generate the signed-lane conformance vectors, from this repo's own implementation.

Why this exists: the signed lane is the only lane where `from` means anything, and getting
onto it requires a client to reproduce three things byte-exactly before a signature will
verify — the single-line sweep (`store.clean_text`), the `did:key` encoding
(`didkey.public_key`), and the canonical payload `<room>|<nonce>|<swept text>`. Get any of
them wrong and the server answers 403 with no hint which one it was, because a signature is
pass/fail and carries no diagnosis. Third-party clients now exist in other languages, and
nothing tells them whether they agree with this server.

So: emit the agreement as data. `vectors.json` is generated FROM the implementation, never
hand-written, and `test_conformance.py` asserts the implementation still matches it — so the
file cannot drift from the server, and a change that moves the boundary fails CI here rather
than in someone else's client.

    python tests/conformance/generate_vectors.py           # rewrite vectors.json
    python tests/conformance/generate_vectors.py --check    # exit 1 if it would change

Two representation decisions worth stating, because they are the difference between a vector
file that ports and one that only works in Python:

  - **Text is carried as code points, not as a JSON string.** The sweep's whole job is
    characters that cannot survive a round trip: a lone surrogate (`Cs`) has no UTF-8
    encoding at all, so a JSON string literally cannot hold one. Every case therefore
    carries `in_cp`/`out_cp` — arrays of integers — as the authoritative form, plus a
    lossy `in_display` for human reading. A client reconstructs from the code points.

  - **The Unicode version is recorded.** The sweep is `unicodedata.category(c) in
    ("Cc","Cf","Cs","Co","Zl","Zp")`, so its result is a function of the Unicode tables the
    runtime ships, not of this code. U+180E moved Zs -> Cf in Unicode 6.3; a JS runtime
    evaluating `\\p{Cf}` uses its own tables. A vector that disagrees across versions is a
    fact about that character, not a bug, and `version_sensitive: true` marks the ones where
    that is the expected answer.

The two traps these vectors exist to catch, both live in the tracker:

  - **Surrogates.** Python iterates a `str` by code point, so U+1F680 is one character of
    category `So` and survives. A client that iterates UTF-16 *code units* — JavaScript's
    `text.split('')`, or `for (let i = 0; i < text.length; i++)` — sees two characters of
    category `Cs` and replaces each with a space. Every astral character then signs the
    wrong bytes. `Array.from(text)` / `[...text]` is the correct iteration.
  - **Signature spelling.** 64 raw bytes is 86 unpadded base64url characters carrying 516
    bits, so the final 4 bits are unconstrained and every signature has SIXTEEN valid
    encodings (issue #177). `didkey.verify` accepts all of them: it pads with "==" and
    decodes. Two clients signing the same message can therefore emit different `sig` strings
    that both verify, which anything comparing signatures as strings must know.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import didkey  # noqa: E402

VECTORS = Path(__file__).resolve().parent / "vectors.json"

# store.py imports fcntl, so it will not import on Windows — where a client author checking
# these vectors is quite likely to be. The sweep is three lines and is replicated verbatim
# below rather than skipping generation on that platform; `_assert_sweep_matches` pins the
# replica against the real thing wherever store CAN be imported, which includes CI.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
MAX_TEXT_CHARS = 4096


def sweep(text: str) -> str:
    """`store.clean_text`'s transform, minus its raises. Verbatim: replace every character
    in INVISIBLE_CATEGORIES with a space, then strip."""
    return "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()


def _assert_sweep_matches() -> None:
    """Ground the replica against the server's own function, or raise.

    Not optional, and not recorded in the file: the generator may only write vectors it has
    checked against the real `clean_text`, so a `vectors.json` that exists is one that agreed
    with the server at the moment it was written. `--allow-unverified` exists for reading
    around on a platform where `store` will not import, and refuses to write.
    """
    import store  # noqa: PLC0415

    for case in SWEEP_CASES:
        text = "".join(chr(c) for c in case["in_cp"])
        want = sweep(text)
        if not want:
            continue  # store.clean_text raises here; the replica returning "" is the vector
        got = store.clean_text(text)
        assert got == want, (case["name"], [ord(c) for c in got], [ord(c) for c in want])
    assert INVISIBLE_CATEGORIES == store.INVISIBLE_CATEGORIES
    assert MAX_TEXT_CHARS == store.MAX_TEXT_CHARS


def cp(text: str) -> list[int]:
    return [ord(c) for c in text]


def display(text: str) -> str:
    """Best-effort human form. Lone surrogates cannot be encoded, so they are escaped; the
    code points remain authoritative either way."""
    return "".join(c if unicodedata.category(c) != "Cs" else f"\\u{ord(c):04x}" for c in text)


# ---------------------------------------------------------------------------- sweep cases


def _case(name: str, text: str, note: str, *, version_sensitive: bool = False) -> dict:
    out = sweep(text)
    return {
        "name": name,
        "in_cp": cp(text),
        "in_display": display(text),
        "out_cp": cp(out),
        "out_display": display(out),
        "raises_empty": out == "",
        "version_sensitive": version_sensitive,
        "note": note,
    }


SWEEP_CASES = [
    _case("ascii-unchanged", "hello bob", "the identity case: nothing to sweep"),
    _case("newline-Cc", "a\nb", "Cc -> space. One record is one line, for every reader"),
    _case("tab-and-cr-Cc", "a\tb\r\nc", "each swept character costs exactly one space"),
    _case(
        "leading-trailing-trimmed",
        "  padded  ",
        "the strip is part of the transform: sign the swept AND trimmed text",
    ),
    _case(
        "swept-then-trimmed",
        "​hi​",
        "U+200B is Cf, so it becomes a space and the strip then removes it — the two steps "
        "compose, and a client that trims first gets a different answer",
    ),
    _case(
        "astral-emoji-SURVIVES",
        "ship \U0001f680 it",
        "THE SURROGATE TRAP. U+1F680 is one code point of category So and is preserved. A "
        "UTF-16 code-unit iterator sees D83D+DE80, both category Cs, and emits two spaces",
    ),
    _case(
        "zwj-family-flattens",
        "\U0001f468‍\U0001f469‍\U0001f467",
        "the README's documented trade: ZWJ is Cf, so the sequence flattens to three "
        "separate emoji joined by a space. Mangled emoji is visible; a smuggled "
        "instruction would not be",
    ),
    _case(
        "zwnj-orthographic",
        "می‌خوام",
        "issue #144/#158: U+200C is orthographic in Perso-Arabic and Brahmic scripts, so "
        "the stored text differs from the sent text. Sign the swept form regardless",
    ),
    _case("bidi-override-Cf", "a‮b", "U+202E is Cf — a rendering attack, swept"),
    _case("bom-Cf", "﻿hi", "U+FEFF is Cf -> space -> stripped. Not str.isspace() in Python"),
    _case("line-separator-Zl", "a b", "U+2028 is Zl"),
    _case("paragraph-separator-Zp", "a b", "U+2029 is Zp"),
    _case("private-use-Co", "ab", "U+E000 is Co — renders as nothing portable"),
    _case(
        "lone-surrogate-Cs",
        "a\ud800b",
        "an unpaired surrogate is Cs -> space. Note it has no UTF-8 encoding, so this input "
        "cannot appear in a JSON string at all — which is why in_cp is authoritative",
    ),
    _case(
        "nbsp-Zs-KEPT",
        "a b",
        "U+00A0 is Zs, and Zs is NOT in the swept set — an interior NBSP survives. A client "
        "sweeping 'all whitespace' rather than these six categories signs the wrong text",
    ),
    _case(
        "nbsp-edges-stripped",
        " hi ",
        "kept by the sweep (Zs), then removed by str.strip(), which does strip U+00A0. JS "
        "String.trim() agrees here — but the two differ on other characters, so trim the "
        "swept output rather than assuming the definitions match",
    ),
    _case(
        "mongolian-vowel-separator",
        "a᠎b",
        "U+180E was Zs before Unicode 6.3 and is Cf after. The answer depends on the "
        "runtime's tables, not on this code",
        version_sensitive=True,
    ),
    _case(
        "combining-marks-kept",
        "ȩ́",
        "Mn is not swept: no normalisation happens anywhere, so decomposed and precomposed "
        "forms are different messages with different signatures",
    ),
    _case(
        "all-invisible-empties",
        "‍‌﻿",
        "everything sweeps to spaces and the strip leaves nothing — store.clean_text raises "
        "here rather than writing a blank record",
    ),
]


# --------------------------------------------------------------------------- did:key cases


def b58encode(raw: bytes) -> str:
    """base58btc. Leading zero bytes become '1's — which never happens for an ed25519-pub
    did:key, since the multicodec prefix starts 0xed, but a general encoder owes it (the
    mirror of issue #155 on the decode side)."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = didkey._B58[rem] + out
    return didkey._B58[0] * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def did_for(public: bytes) -> str:
    mb = b58encode(didkey.MULTICODEC_ED25519 + public)
    assert len(mb) == didkey.MULTIBASE_CHARS - 1, len(mb)
    did = f"{didkey.PREFIX}z{mb}"
    assert didkey.public_key(did) == public  # the generator checks its own output
    return did


def fingerprint(did: str) -> str:
    """The first 16 lowercase hex characters of SHA-256 over the DID string."""
    return hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]


def _invalid(did: str, why: str) -> dict:
    try:
        didkey.public_key(did)
    except didkey.DidError as exc:
        return {"did": did, "why": why, "error": str(exc)}
    raise AssertionError(f"expected {why!r} to be rejected: {did}")


# ------------------------------------------------------------------------- signature cases


def sig_variants(signature: str) -> list[str]:
    """Every base64url spelling of the same 64 bytes (issue #177).

    64 bytes is 21 full 3-byte groups plus one leftover byte, so the tail is 2 characters
    holding 12 bits for 8 bits of data. The final character's low 4 bits are unused, and
    `verify` pads with "==" and decodes without checking them — so sixteen distinct strings
    denote one signature. Returned canonical-first.
    """
    raw = base64.urlsafe_b64decode(signature + "==")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    base = (raw[-1] & 0x03) << 4
    out = []
    for free in range(16):
        candidate = signature[:-1] + alphabet[base + free]
        assert base64.urlsafe_b64decode(candidate + "==") == raw
        out.append(candidate)
    assert signature in out, "canonical spelling must be one of the sixteen"
    return [signature] + [s for s in out if s != signature]


def signature_cases() -> list[dict]:
    from nacl.signing import SigningKey  # noqa: PLC0415

    cases = []
    for label, seed, room, nonce, raw_text in (
        ("ascii", b"\x01" * 32, "lobby", 1, "hello bob"),
        ("swept-newline", b"\x02" * 32, "lobby", 2, "two\nlines"),
        ("astral-emoji", b"\x03" * 32, "d-mine", 1_724_000_000_000, "ship \U0001f680 it"),
        ("zwnj-perso-arabic", b"\x04" * 32, "meta", 42, "می‌خوام"),
        ("nbsp-interior-kept", b"\x05" * 32, "lobby", 7, "a b"),
    ):
        key = SigningKey(seed)
        public = bytes(key.verify_key)
        did = did_for(public)
        text = sweep(raw_text)
        payload = f"{room}|{nonce}|{text}"
        signature = base64.urlsafe_b64encode(key.sign(payload.encode("utf-8")).signature)
        signature = signature.decode().rstrip("=")
        assert len(signature) == didkey.SIG_CHARS, len(signature)
        didkey.verify(did, signature, payload)  # the generator proves its own vector
        variants = sig_variants(signature)
        for spelling in variants:
            didkey.verify(did, spelling, payload)  # all sixteen really are accepted
        cases.append(
            {
                "name": label,
                "seed_hex": seed.hex(),
                "did": did,
                "room": room,
                "nonce": nonce,
                "text_raw_cp": cp(raw_text),
                "text_swept_cp": cp(text),
                "payload_display": display(payload),
                "payload_utf8_hex": payload.encode("utf-8").hex(),
                "sig_canonical": signature,
                "sig_accepted_spellings": variants,
                "note": "sign the SWEPT text. payload is <room>|<nonce>|<swept text>, UTF-8. "
                "seq and ts are the server's and are not covered",
            }
        )
    return cases


# ------------------------------------------------------------------------------------ main


def build() -> dict:
    """The vector file's contents.

    Everything here is a function of the implementation and of the Unicode tables — and of
    nothing else. No timestamp, no Python version, no hostname: a file that changes when
    nothing changed produces diffs nobody reads, and `test_vectors_are_not_stale` compares
    this whole dict, so any environment-dependent field would make CI fail for the wrong
    reason. `unicode_version` is the one environmental fact recorded, because it genuinely
    determines the answers (see `version_sensitive`) — and it is the reason CI pins a Python
    version rather than tracking latest.
    """
    from nacl.signing import SigningKey  # noqa: PLC0415

    identities = []
    for seed in (bytes([i]) * 32 for i in (1, 2, 3)):
        did = did_for(bytes(SigningKey(seed).verify_key))
        fp = fingerprint(did)
        identities.append(
            {
                "seed_hex": seed.hex(),
                "did": did,
                "fingerprint": fp,
                "sharded_write_path": f"/kv/did-{fp[:2]}/{fp[2:]}",
                "legacy_read_path": f"/kv/did/{fp}",
                "note": "new notes go to the sharded path; the legacy one is the read "
                "fallback and is full at its cap on technocore.chat",
            }
        )
    return {
        "$comment": "GENERATED by tests/conformance/generate_vectors.py — do not hand-edit. "
        "Text is carried as code points because the swept characters include some with no "
        "UTF-8 encoding.",
        "provenance": {
            "unicode_version": unicodedata.unidata_version,
            "invisible_categories": list(INVISIBLE_CATEGORIES),
            "max_text_chars": MAX_TEXT_CHARS,
            "did_pattern": didkey.DID_PATTERN,
            "sig_pattern": didkey.SIG_PATTERN,
            "nonce_pattern": didkey.NONCE_PATTERN,
        },
        "sweep_cases": SWEEP_CASES,
        "identities": identities,
        "did_invalid": [
            _invalid("z6MkfooBAR", "missing the did:key: prefix"),
            _invalid(didkey.PREFIX + "q6Mk" + "1" * 44, "multibase tag is 'q', not 'z'"),
            _invalid(didkey.PREFIX + "z6Mk" + "1" * 43, "47 multibase characters, not 48"),
            _invalid(didkey.PREFIX + "z6Mk" + "1" * 45, "49 multibase characters, not 48"),
            _invalid(didkey.PREFIX + "z6Mk" + "0" * 44, "'0' is not in the base58btc alphabet"),
            _invalid(didkey.PREFIX + "z6Mk" + "l" * 44, "'l' is not in the base58btc alphabet"),
            _invalid(didkey.PREFIX + "z" + "1" * 47, "decodes to 1 byte, not 34"),
            _invalid(
                didkey.PREFIX + "z" + b58encode(b"\xec\x01" + b"\x00" * 32),
                "multicodec is not ed25519-pub (0xed01)",
            ),
        ],
        "signature_cases": signature_cases(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if vectors.json is stale")
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="build even where `store` will not import (its fcntl is POSIX-only). Prints the "
        "vectors and refuses to write them: an unverified vector file is worse than none, "
        "because a client that trusts one has no way to find out.",
    )
    args = parser.parse_args()

    try:
        _assert_sweep_matches()
        verified = True
    except ImportError as exc:
        if not args.allow_unverified:
            raise SystemExit(
                f"cannot import store ({exc}), so the sweep replica in this file is "
                f"unchecked.\nRegenerate where store imports — its fcntl is POSIX-only — or "
                f"pass --allow-unverified to build without writing."
            ) from None
        verified = False

    built = build()
    text = json.dumps(built, indent=2, ensure_ascii=True) + "\n"

    if args.check:
        current = VECTORS.read_text(encoding="utf-8") if VECTORS.exists() else ""
        if current != text:
            print("vectors.json is stale — re-run without --check", file=sys.stderr)
            raise SystemExit(1)
        print("vectors.json is current")
        return

    prov = built["provenance"]
    summary = (
        f"  unicode {prov['unicode_version']}\n"
        f"  {len(built['sweep_cases'])} sweep cases "
        f"({sum(1 for c in built['sweep_cases'] if c['version_sensitive'])} version-sensitive, "
        f"{sum(1 for c in built['sweep_cases'] if c['raises_empty'])} empty-after-sweep)\n"
        f"  {len(built['identities'])} identities  ·  "
        f"{len(built['did_invalid'])} rejected DID shapes\n"
        f"  {len(built['signature_cases'])} signature cases × "
        f"{len(built['signature_cases'][0]['sig_accepted_spellings'])} accepted spellings each"
    )
    if not verified:
        print("NOT WRITING: the sweep was never checked against store.clean_text.")
        print("Would have written:")
        print(summary)
        raise SystemExit(1)

    VECTORS.write_text(text, encoding="utf-8")
    print(f"wrote {VECTORS.name}, verified against store.clean_text")
    print(summary)


if __name__ == "__main__":
    main()
