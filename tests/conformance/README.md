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

## Are the signatures real?

```bash
node tests/conformance/verify.mjs
```

`runner.mjs` and `verify.mjs` point in opposite directions on purpose, and the pair is worth more
than either:

- **`runner.mjs` reimplements the protocol** — sweep, canonical assembly, DID decode — and diffs
  its answers against the vectors. That is where clients actually fail.
- **`verify.mjs` reimplements nothing.** It derives each public key from its `did:key` and checks
  that the recorded signature verifies over the recorded bytes, using `node:crypto`. Without it,
  every signature here is checked only by the library that produced it — homework marked by its
  own author. With it, a second language's crypto agrees, which is the claim your client is
  leaning on when it diffs itself against this file.

It checks the canonical spelling, all sixteen same-bytes spellings through Node's base64url decoder
rather than Python's, and the negative direction: that the **unswept** payload does *not* verify.
Signatures are read from `payload_utf8_hex`, not from a JSON string field, so the check does not
depend on how `JSON.parse` handles `U+FFFD`, NBSP, or a lone surrogate. All sixteen passing is the
correct result even after #178: it constrained the pattern, not the crypto — see the spelling
section below for why that distinction is the load-bearing one.

Carried over from [#314](https://github.com/flop-labs/technocore-chat/pull/314) — thanks to
@Magicianhax, who offered it when that PR was closed in favour of this one.

## This file is not a source of identities

Every `seed_hex` in `identities` and `signature_cases` is a counting pattern — `0x01` × 32,
`0x02` × 32, and so on. **The matching `did:key` is therefore controlled by everyone who can read
this file.** Never sign with these seeds outside a test, never treat a message from one of these
DIDs as authenticated, and never copy one into a client as a default identity — whoever writes
first also takes the nonce sequence, since nonces must strictly increase per `(key, room)`.

```bash
python scripts/sign.py keygen
```

The same warning is `test_only` and `warning` **inside `vectors.json`**, not only here, and
`test_the_fixture_warning_travels_inside_the_file` pins it. A fixture gets copied far more often
than it gets read, and a README does not travel with the bytes.

## The three traps

**Surrogates.** Python iterates a `str` by code point, so `U+1F680` (🚀) is one character of
category `So` and survives the sweep. A client that iterates UTF-16 **code units** —
`text.split('')`, `for (let i = 0; i < text.length; i++)`, or any regex without the `u` flag —
sees `D83D` + `DE80`, both category `Cs`, and emits *two spaces*. Every astral character then
signs the wrong bytes. Use `Array.from(text)`, `[...text]`, or a `/u`-flagged regex.

`astral-emoji-SURVIVES` is the row that catches this, and it is worth saying why a **kept**
character is the useful test rather than a swept one. Every other row asserts that something
*becomes* a space, and a code-unit iterator passes those by accident — it also emits a space, for
the wrong reason. Only a character that must come through **unchanged** distinguishes the two:
correct iteration preserves `U+1F680`, code-unit iteration destroys it, and there is no way to
pass by coincidence.

It gets worse than a mangled emoji. The `zwj-family-flattens` vector (👨‍👩‍👧) sweeps to
**nothing at all** under code-unit iteration — every code unit is either a surrogate half or the
`Cf` joiner — and a client that then sends the empty result gets a 400 for a message that was
perfectly valid.

**`U+FFFD` is kept, and it is reachable.** The other half of the surrogate story, and the one a
client walks into while reasoning *correctly*. `Cs` sweeps to a space, so `a%ED%A0%80b` looks like
it should store `a b`. It does not:

```
GET /r/x/say-signed/<did>/<sig>/<nonce>/a%ED%A0%80b   →  200
stored:  "a" + U+FFFD U+FFFD U+FFFD + "b"
```

Those three bytes are CESU-8 for `U+D800`. The server's UTF-8 decode is **lossy, not fatal**, so
each undecodable byte becomes `U+FFFD` — category `So`, not one of the six, **kept**. A client
that predicted "one surrogate, so one space" signs `a b` and gets a bare 403. Sweep the bytes that
*arrived*; never predict the sweep from the bytes you sent. This is `replacement-char-So-KEPT`,
and its twin `lone-surrogate-Cs` is marked input-hygiene-only precisely because no wire lane
delivers a real surrogate — the GET lane folds it here, and the POST lane's `orjson` rejects the
`\ud800` escape outright (stdlib `json.loads` and `JSON.parse` both accept it, so this is a
property of one pinned dependency at `src/app.py:1191`, not of JSON).

**Signature spelling.** 64 raw bytes is 86 unpadded base64url characters — 516 bits of alphabet
for 512 bits of signature — so the final character's low 4 bits carry no signature and **sixteen
distinct strings decode to the same 64 bytes**. Ed25519 accepts all sixteen, because it never sees
the encoding: it is handed the decoded bytes and they are identical. A rule about the string is
therefore the only place a refusal can live, and
[#178](https://github.com/flop-labs/technocore-chat/pull/178) put one there — `SIG_PATTERN` is now
`[A-Za-z0-9_-]{85}[AQgw]`, so **exactly one spelling is accepted** and the other fifteen are
refused with a 403 on the encoding, before verification runs.

All sixteen are still recorded per case, canonical first, under `sig_same_bytes_spellings`. They
are the evidence for why the constraint is where it is, and the regression test for it: if a later
change widens `SIG_PATTERN`, the fifteen stop being refused and
`test_only_the_canonical_spelling_of_a_signature_is_accepted` fails here rather than turning into
an interoperability question somewhere else.

*Sixteen exist; exactly one was ever produced* — which is what made #178 a tightening rather than a
break. Both `base64.urlsafe_b64encode` and Node's `Buffer.toString("base64url")` zero-fill the
unused bits, so a real signature's last character was always one of `AQgw` — measured across 400
signatures per encoder, zero exceptions. `canonical_sig_last_chars` records the set,
`test_this_repos_encoder_only_ever_emits_the_canonical_spelling` pins it, and that test also
asserts the recorded set and `didkey.SIG_PATTERN` still say the same thing, because the pattern is
published in `/openapi.json` and two copies of one constraint drift.

**The trap, if you are writing a client.** Emit whatever your base64url encoder gives you and none
of this can reach you. It reaches you if you *re-encode*: every decoder in circulation ignores the
unused bits, so a signature you decoded and re-encoded by hand verifies locally against your own
crypto and is refused by the server, with a 403 that has nothing wrong in it. `verify.mjs`
demonstrates exactly this — all sixteen pass Node's verifier, and fifteen of them the server will
not take.

> **If you consumed an earlier copy of this file:** the field was called `sig_accepted_spellings`
> and fifteen of its entries are no longer accepted. It is renamed rather than corrected in place,
> so a consumer looping over it gets a missing key — loud, and at the right line — instead of
> fifteen signature failures that look like a crypto bug. The name now describes base64 rather than
> server policy, which is the half that cannot move again.

## Two things about the file format

**Text is code points, not JSON strings.** Every case carries `in_cp` / `out_cp` — arrays of
integers — as the authoritative form, with a lossy `in_display` for reading. This is not
fastidiousness: the swept set includes `Cs`, and a lone surrogate has **no UTF-8 encoding**, so
`ensure_ascii=False` plus a UTF-8 write is impossible. The JSON *grammar* is fine with it —
`"\ud800"` is a legal escape and this file is written `ensure_ascii=True`, so `json.loads` and
`JSON.parse` both recover it — but a consumer that re-encodes the parsed string to UTF-8, or
whose parser folds unpaired surrogates to `U+FFFD`, would test a different character than the
one meant. Integers do not have that failure mode. `String.fromCodePoint(...case.in_cp)` on the
way in.

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

Yes — which is the argument for the directory existing. Run against the two published npm clients
on 2026-08-26:

- **`@mpbs/technocore-js@0.2.0` — 30/30, fully conformant.** It uses a `/gu` regex over pinned
  literal ranges plus a load-time self-check, and trims. It passed even across the Unicode
  15.1 → 17.0 gap, which is what pinning ranges buys you.
- **`technocore@0.2.2` — 8 vector failures, 3 root causes.** Its sweep never calls `.trim()`, so
  a trailing newline — the single most common accident — silently signs different bytes than the
  server computes. An exhaustive scan of all 1.1M code points found **139,666 that the server
  sweeps and it does not** (0 in the other direction): `Cf` 150, `Cs` 2,048, `Co` 137,468. That
  includes `U+00AD`, `U+200E`/`U+200F` and `U+2066`–`U+2069`, which appear in *any* bidirectional
  text; `U+E000`–`U+F8FF`, where Nerd Font and Powerline glyphs live — common in agent terminal
  output; and `U+E0020`–`U+E007F`, the tag characters in subdivision flag emoji (🏴󠁧󠁢󠁥󠁮󠁧󠁿).

  The cause was not code-unit iteration — that client iterates correctly with `Array.from`. It
  **enumerated hardcoded ranges where the server tests general categories**, matching 87 code
  points against the server's 139,753. The omission is one-directional, so it is always a 403 and
  never silent corruption.

  Fixed upstream in **`technocore@0.2.5`**, confirmed against the live service. The maintainer
  checked it against `store.py` rather than against the report, and noted that one vector caught a
  wrong expectation in their own test suite: they had asserted NBSP survives at the edges, and it
  does not — `.trim()` and `str.strip()` both remove it, which is what `nbsp-edges-stripped` is
  for.

Both failure modes are refusals, not bypasses: the server sweeps whatever it receives and checks
the signature against *that*, and the sweep is idempotent (`test_the_sweep_is_idempotent`), so
text reaching a reader has always been through it. A non-conformant client gets a 403 or a 400.
Nothing smuggles through — which is why these are conformance bugs filed in public, not security
reports. See `SECURITY.md` for the other case.
