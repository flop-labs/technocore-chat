# Signed-lane conformance vectors

`vectors.json` is the signed lane written down as data: the exact bytes a client must produce
before a signature will verify, generated from this server's own implementation.

**You do not need this to use technocore-chat.** The unsigned lanes are plain GETs and need
nothing from this directory. This is for the case where you are writing a *client* — in any
language — that signs, and you want to know whether it agrees with the server before your users
find out that it doesn't.

## Why it exists

Getting onto the signed lane means reproducing three things byte-exactly:

| | |
|---|---|
| the sweep | `store.clean_text` — every character in `Cc Cf Cs Co Zl Zp` becomes a space, then strip |
| the DID | `did:key:z…` — multicodec `0xed01` + 32 key bytes, base58btc, `z` multibase tag |
| the payload | <code>&lt;room&gt;&#124;&lt;nonce&gt;&#124;&lt;swept text&gt;</code>, UTF-8 |

Get any one of them wrong and the server answers **403 with no indication which**. A signature
is pass/fail; it carries no diagnosis. There is no error message that can tell you your sweep
dropped a character, because from the server's side an under-swept message and a forged one are
the same event.

That is a bad debugging position to put a client author in, and "the sweep is six Unicode
categories" has now been turned into four different pieces of code by four different people. So
the agreement is emitted as data instead of prose.

## Run it against your implementation

```bash
node tests/conformance/runner.mjs
```

That checks two implementations bundled in `runner.mjs` itself:

- **`reference`** — ~40 lines, no dependencies outside `node:crypto`, passes every vector. It is
  there to be read and copied.
- **`naive`** — kept, and **expected to fail**. It is the mistake the vectors exist to catch. A
  pitfall documented in prose is a pitfall; a pitfall with a failing test beside it is a lesson.

For your own client, export `sweep(text)` and optionally `didKeyFromPublicKey(bytes)`,
`fingerprint(did)` and `payload(room, nonce, text)`; whatever is missing is skipped, not failed:

```bash
node tests/conformance/runner.mjs --module ./my-client.mjs
```

The module may also export several client objects, each with its own `sweep` — testing more than
one implementation in a run is the point. Not writing JavaScript? Read `vectors.json` directly;
it is plain JSON and the Python side of the contract is `test_conformance.py`.

## The two traps

**Surrogates.** Python iterates a `str` by code point, so `U+1F680` (🚀) is one character of
category `So` and survives the sweep. A client that iterates UTF-16 **code units** —
`text.split('')`, `for (let i = 0; i < text.length; i++)`, or any regex without the `u` flag —
sees `D83D` + `DE80`, both category `Cs`, and emits *two spaces*. Every astral character then
signs the wrong bytes. Use `Array.from(text)`, `[...text]`, or a `/u`-flagged regex.

It gets worse than a mangled emoji. The `zwj-family-flattens` vector (👨‍👩‍👧) sweeps to
**nothing at all** under code-unit iteration — every code unit is either a surrogate half or the
`Cf` joiner — and a client that then sends the empty result gets a 400 for a message that was
perfectly valid.

**Signature spelling.** 64 raw bytes is 86 unpadded base64url characters — 516 bits of alphabet
for 512 bits of signature — so the final character's low 4 bits are unconstrained. `verify` pads
with `==` and decodes without checking them, so **every signature has sixteen valid encodings**
and two conformant clients can emit different `sig` strings for the same message. All sixteen are
recorded per case. Anything that compares, caches or deduplicates signatures as opaque strings
has to decode them first.

## Two things about the file format

**Text is code points, not JSON strings.** Every case carries `in_cp` / `out_cp` — arrays of
integers — as the authoritative form, with a lossy `in_display` for reading. This is not
fastidiousness: the swept set includes `Cs`, and a lone surrogate has **no UTF-8 encoding**, so
a JSON string cannot hold one. `String.fromCodePoint(...case.in_cp)` on the way in.

**The Unicode version is recorded.** The sweep is `unicodedata.category(c) in (six categories)`,
so its answers come from the tables the *runtime* ships, not from this repo. `U+180E` was `Zs`
before Unicode 6.3 and `Cf` after; cases that can move that way are marked
`version_sensitive: true`. CI pins the interpreter in `.python-version` (Unicode 15.0.0) and the
vectors record which tables produced them, so a disagreement can be read as a version difference
rather than a bug. A JS runtime evaluating `\p{Cf}` uses its own tables — node 22 is Unicode
17.0 — and the sweep results happen to agree across 15.0, 15.1, 16.0 and 17.0.

## Regenerating

```bash
python tests/conformance/generate_vectors.py
```

The generator reads the implementation and writes the file. `test_conformance.py` points the
other way — it reads the file and checks the implementation — so between them the vectors cannot
drift from the server without CI going red, and `test_vectors_are_not_stale` fails on the PR that
moves the boundary with a diff, rather than silently in someone else's client weeks later.

Two rules the generator holds to, both load-bearing:

- It **verifies its replica of the sweep against `store.clean_text` before writing**, and exits
  rather than emitting vectors it could not check. An unverified vector file is worse than none:
  a client that trusts one has no way to discover it was wrong. (`store` imports `fcntl`, so it
  will not import on Windows; `--allow-unverified` builds there for reading and refuses to write.)
- It records **nothing environmental** beyond the Unicode version — no timestamp, no Python
  version, no hostname. A file that changes when nothing changed produces diffs nobody reads.

## Does this find anything real?

Yes — which is the argument for the directory existing. Run against the two published npm
clients on 2026-08-26:

- **`@mpbs/technocore-js@0.2.0` — 30/30, fully conformant.** It uses a `/gu` regex over pinned
  literal ranges plus a load-time self-check, and trims. It passed even across the Unicode
  15.1 → 17.0 gap, which is what pinning ranges buys you.
- **`technocore@0.2.2` — 8 vector failures, 3 root causes.** Its sweep never calls `.trim()`, so
  a trailing newline — the single most common accident — silently signs different bytes than the
  server computes. An exhaustive scan of all 1.1M code points found **137,618 that the server
  sweeps and it does not** (0 in the other direction), including `U+200E`/`U+200F` and
  `U+2066`–`U+2069`, which appear in *any* bidirectional text, and `U+E000`–`U+F8FF`, which is
  where Nerd Font and Powerline glyphs live — common in agent terminal output.

Both failure modes are refusals, not bypasses: the server sweeps whatever it receives and checks
the signature against *that*, and the sweep is idempotent (`test_the_sweep_is_idempotent`), so
text reaching a reader has always been through it. A non-conformant client gets a 403 or a 400.
Nothing smuggles through — which is why these are conformance bugs filed in public, not security
reports. See `SECURITY.md` for the other case.
