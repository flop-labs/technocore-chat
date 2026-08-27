/**
 * Re-verify the conformance vectors' signatures from a second language.
 *
 * Carried over from @Magicianhax's #314 (Apache-2.0, offered explicitly when that PR was
 * closed in favour of this one) and adapted to this file's schema. The idea is theirs and it
 * is the half `runner.mjs` deliberately does not cover:
 *
 *   - `runner.mjs` REIMPLEMENTS the protocol — sweep, canonical assembly, DID decode — and
 *     diffs its answers against the vectors. That is where clients actually fail, and it
 *     carries a naive sweep beside the correct one so the failure is legible.
 *   - `verify.mjs` reimplements NOTHING. It derives each public key from its `did:key` and
 *     checks that the recorded signature verifies over the recorded bytes. If it passes, the
 *     signatures in this directory are real Ed25519 signatures that a second language's
 *     crypto agrees on — rather than self-marked homework from the generator that wrote them.
 *
 * The one adaptation worth naming: signatures are checked against `payload_utf8_hex`, not
 * against a JSON string field. The payload can contain U+FFFD and NBSP and, in the sweep
 * cases, an unpaired surrogate; reading the bytes as hex means this check does not depend on
 * how `JSON.parse` treats any of them, which is the same reason the sweep cases carry
 * `in_cp`. `payload_display` exists for humans and is not used here.
 *
 *     node tests/conformance/verify.mjs
 *
 * Exit 0 = every signature verifies. Node stdlib only. `test_conformance.py` shells out to
 * this when a `node` binary is present and skips it otherwise, because CI is pure-Python.
 */

import { readFileSync } from 'node:fs';
import { createPublicKey, verify } from 'node:crypto';

const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const B58_INDEX = Object.fromEntries([...B58].map((c, i) => [c, i]));

function b58decode(str) {
  let n = 0n;
  for (const ch of str) {
    const d = B58_INDEX[ch];
    if (d === undefined) throw new Error(`bad base58btc char ${JSON.stringify(ch)}`);
    n = n * 58n + BigInt(d);
  }
  const body = [];
  while (n > 0n) {
    body.unshift(Number(n % 256n));
    n /= 256n;
  }
  // Leading zero bytes encode as '1's and carry no value, so they survive the BigInt round
  // trip only if counted separately. An ed25519-pub did:key never has one — the multicodec
  // starts 0xed — but a decoder that drops them is wrong in general, and issue #155 is the
  // same bug on this repo's own decode side.
  let zeros = 0;
  for (const ch of str) {
    if (ch === '1') zeros++;
    else break;
  }
  return Buffer.from([...Array(zeros).fill(0), ...body]);
}

function verifyKey(did) {
  const mb = did.slice('did:key:'.length);
  const decoded = b58decode(mb.slice(1)); // drop the multibase 'z'
  if (decoded.length !== 34) throw new Error(`${did}: ${decoded.length} bytes, expected 34`);
  if (decoded[0] !== 0xed || decoded[1] !== 0x01) throw new Error(`${did}: not ed25519-pub`);
  const raw = decoded.subarray(2);
  // 32 raw bytes wrapped in the fixed DER SPKI prefix for Ed25519, which is what
  // node:crypto will accept as a verify key.
  const spki = Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), raw]);
  return createPublicKey({ key: spki, format: 'der', type: 'spki' });
}

const v = JSON.parse(readFileSync(new URL('./vectors.json', import.meta.url)));

let checked = 0;
let fail = 0;
const check = (name, key, payload, sig) => {
  checked++;
  if (!verify(null, payload, key, Buffer.from(sig, 'base64url'))) {
    fail++;
    console.error(`FAIL ${name}`);
  }
};

for (const c of v.signature_cases) {
  const key = verifyKey(c.did);
  const payload = Buffer.from(c.payload_utf8_hex, 'hex');
  check(`${c.name} canonical`, key, payload, c.sig_canonical);
  // All sixteen spellings, from Node's decoder rather than Python's. `Buffer.from(s,
  // 'base64url')' ignores the unused trailing bits exactly as `base64.urlsafe_b64decode`
  // does, so a divergence here would mean the sixteen-spellings claim is Python-specific.
  for (const spelling of c.sig_accepted_spellings) {
    check(`${c.name} spelling ${spelling.slice(-1)}`, key, payload, spelling);
  }
  // The negative direction, which is the property clients care about: the signature is over
  // the SWEPT text, so the raw text must not verify.
  const raw = Buffer.from(String.fromCodePoint(...c.text_raw_cp), 'utf8');
  const swept = Buffer.from(String.fromCodePoint(...c.text_swept_cp), 'utf8');
  if (!raw.equals(swept)) {
    const wrong = Buffer.from(`${c.room}|${c.nonce}|${String.fromCodePoint(...c.text_raw_cp)}`, 'utf8');
    checked++;
    if (verify(null, wrong, key, Buffer.from(c.sig_canonical, 'base64url'))) {
      fail++;
      console.error(`FAIL ${c.name}: the UNSWEPT payload verified, which it must not`);
    }
  }
}

if (fail === 0) console.log(`ok: ${checked} signature checks pass from Node`);
process.exit(fail === 0 ? 0 : 1);
