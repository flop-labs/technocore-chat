# scripts/

Standalone signing helpers for technocore-chat's signed (`did:key`) lane.
Both scripts are independent, dependency-minimal, and produce byte-identical
output for the same seed — pick whichever fits your runtime.

## sign.py

Requires Python 3.12+. Uses [`uv`](https://docs.astral.sh/uv/) to
auto-provision its one dependency (`cryptography`) from the PEP 723 header
at the top of the file — no manual install, no venv:

uv run scripts/sign.py keygen
uv run scripts/sign.py did [--seed HEX|PASSPHRASE]
uv run scripts/sign.py say [--seed ...] <room> <nonce> <text>
uv run scripts/sign.py set [--seed ...] <ns> <key> <nonce> <value>


## sign.js

Requires Node.js 14.18+. Zero dependencies — Node's built-in `crypto` module
has native Ed25519 support, so there is no `npm install`, no
`package.json`, nothing to provision:

node scripts/sign.js keygen
node scripts/sign.js did [--seed HEX|PASSPHRASE]
node scripts/sign.js say [--seed ...] <room> <nonce> <text>
node scripts/sign.js set [--seed ...] <ns> <key> <nonce> <value>


## Compatibility

`sign.js` is a faithful port of `sign.py`, verified byte-for-byte against
it: identical DID derivation (multicodec + base58btc), identical
signatures over the canonical `<room>|<nonce>|<swept-text>` /
`<ns>|<key>|<nonce>|<swept-value>` strings, identical handling of the
single-line Unicode sweep (control characters, format characters,
surrogates, private-use, line/paragraph separators — all become spaces
before signing, matching `src/store.py`'s `clean_text`), and identical
error messages for bad nonces, empty-after-sweep text, and over-limit
input. Same seed in, same `did:key` and signature out, from either script.

Verified by `tests/test_sign_js_parity.py`, which runs both scripts
side by side with matching inputs and asserts identical output.
