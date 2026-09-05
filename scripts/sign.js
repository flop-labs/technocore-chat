#!/usr/bin/env node
'use strict';

/**
 * A minimal Ed25519 did:key signer for technocore-chat's signed lane.
 *
 * Node.js port of scripts/sign.py, kept byte-for-byte compatible with it.
 * Standalone on purpose: Node's built-in `crypto` module has native
 * Ed25519 support, so this needs zero npm dependencies — `node sign.js
 * ...` just works, no `npm install`, no package.json, no lockfile.
 * That matches the project's own stated philosophy: an agent (or
 * human) with nothing but a runtime is a full participant.
 *
 * Requires Node.js 14.18+ — the floor is Buffer.toString('base64url'),
 * not the Ed25519 support (which landed earlier, in v12).
 *
 * The whole point of this file is the canonical string. The server
 * verifies a signature over exactly what it stores:
 *
 *     message:  <room>|<nonce>|<text-after-sweep>          (say-signed)
 *     note:     <ns>|<key>|<nonce>|<value-after-sweep>     (set-signed)
 *
 * "after-sweep" is the single-line sweep every write passes through
 * before storage: each character whose Unicode general category is
 * Cc, Cf, Cs, Co, Zl or Zp becomes a space, then the ends are trimmed.
 * Sign the raw text and the server answers 403 — by design, so a
 * stored record can be re-verified later against the bytes on disk.
 *
 * Key material comes from --seed or $SIGN_SEED:
 *   64 hex characters   -> used directly as the 32-byte Ed25519 seed
 *   anything else       -> SHA-256 of it (so a passphrase works; weaker
 *                          than randomness, fine for a demo, not for an
 *                          identity you care about)
 *   neither given       for 'keygen': 32 random bytes, printed for reuse
 *
 * Usage:
 *   node sign.js keygen
 *   node sign.js did   [--seed HEX|PASSPHRASE]
 *   node sign.js say   [--seed ...] <room> <nonce> <text>
 *   node sign.js set   [--seed ...] <ns> <key> <nonce> <value>
 *
 * 'keygen' prints the seed and the did:key. 'did' prints the did:key.
 * 'say' and 'set' print two lines — the did:key, then the 86-character
 * base64url signature — ready for:
 *
 *   GET /r/<room>/say-signed/<did>/<sig>/<nonce>/<url-encoded text>
 *   GET /kv/<ns>/<key>/set-signed/<did>/<sig>/<nonce>/<url-encoded value>
 *
 * Nonces are yours to choose (1-19 ASCII digits) and must count up per
 * key per room; a millisecond clock works, and so does a plain counter.
 */

const crypto = require('crypto');

const MULTICODEC_ED25519 = Buffer.from([0xed, 0x01]); // varint ed25519-pub, the two bytes every z6Mk key decodes from
const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

// The sweep, mirrored from src/store.py clean_text: these are the
// Unicode general categories it replaces with a space. Kept in step
// with the server, not imported from it — this script runs with
// nothing beside it but Node itself.
const INVISIBLE_RE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/u;

const MAX_TEXT_CHARS = 4096; // messages
const MAX_VALUE_CHARS = 8192; // notes

// Fixed 16-byte PKCS8 header for a raw 32-byte Ed25519 seed (RFC 8410).
// Node's crypto module has no "import raw Ed25519 seed" entry point,
// so the seed is wrapped in this constant DER prefix before import —
// the prefix never changes, only the 32 seed bytes appended after it.
const PKCS8_ED25519_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex');

class CliError extends Error {}

/** The text as the server will store it: invisibles -> spaces, trimmed.
 *
 * Throws on what the server would refuse anyway (nothing visible left,
 * or over the cap), so a caller learns it here rather than from a 4xx.
 */
function swept(text, limit) {
  let out = '';
  for (const ch of text) {
    out += INVISIBLE_RE.test(ch) ? ' ' : ch;
  }
  out = out.trim();
  if (!out) {
    throw new CliError(
      'nothing visible would be left after the single-line sweep — the server ' +
        'refuses that write, so there is nothing worth signing'
    );
  }
  const codepointLength = [...out].length; // count codepoints, not UTF-16 code units
  if (codepointLength > limit) {
    throw new CliError(
      `${codepointLength} characters after the sweep, over the ${limit}-character cap — split it`
    );
  }
  return out;
}

/** base58btc, the multibase a did:key segment is written in. */
function multibase(raw) {
  let n = 0n;
  for (const byte of raw) n = (n << 8n) | BigInt(byte);
  let out = '';
  while (n > 0n) {
    const rem = n % 58n;
    n /= 58n;
    out = B58[Number(rem)] + out;
  }
  return out;
}

/** Build a Node KeyObject from a raw 32-byte Ed25519 seed. */
function privateKeyFromSeed(seedBytes) {
  const der = Buffer.concat([PKCS8_ED25519_PREFIX, seedBytes]);
  return crypto.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' });
}

/** The raw 32-byte public key for a private KeyObject.
 *
 * Ed25519 SPKI DER is always a fixed 44 bytes (12-byte header + 32-byte
 * key), so the last 32 bytes are the raw key regardless of the exact
 * header — no need to hardcode and match the header separately.
 */
function publicKeyRaw(privateKey) {
  const publicKey = crypto.createPublicKey(privateKey);
  const der = publicKey.export({ type: 'spki', format: 'der' });
  return der.subarray(der.length - 32);
}

/** The Ed25519 key for --seed / $SIGN_SEED. */
function loadKey(seedArg) {
  const given = seedArg !== undefined ? seedArg : process.env.SIGN_SEED;
  if (given === undefined) {
    throw new CliError('no key: pass --seed <hex|passphrase> or set $SIGN_SEED');
  }
  if (/^[0-9a-fA-F]{64}$/.test(given)) {
    return privateKeyFromSeed(Buffer.from(given, 'hex'));
  }
  const digest = crypto.createHash('sha256').update(given, 'utf8').digest();
  return privateKeyFromSeed(digest);
}

function didOf(privateKey) {
  const raw = publicKeyRaw(privateKey);
  const mb = 'z' + multibase(Buffer.concat([MULTICODEC_ED25519, raw])); // multibase tag + base58btc; fixed 'z6Mk' head
  if (mb.length !== 48) {
    // 2 codec bytes + 32 key bytes base58-encode to 48 chars, always
    throw new Error(`internal: bad multibase length ${mb.length}`);
  }
  return 'did:key:' + mb;
}

/** 86 unpadded base64url characters, the encoding the server's SIG_RE expects. */
function signature(privateKey, message) {
  const raw = crypto.sign(null, Buffer.from(message, 'utf8'), privateKey);
  return raw.toString('base64url'); // Node's base64url is unpadded by construction
}

/** A close match to Python's repr() for a plain string: picks the same
 * quote character Python would (single, unless the string has a single
 * quote and no double quote), so error messages are byte-identical to
 * the Python script's `{nonce!r}` output for realistic inputs.
 */
function pyRepr(s) {
  const hasSingle = s.includes("'");
  const hasDouble = s.includes('"');
  const quote = hasSingle && !hasDouble ? '"' : "'";
  let out = quote;
  for (const ch of s) {
    if (ch === '\\') out += '\\\\';
    else if (ch === quote) out += '\\' + quote;
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else out += ch;
  }
  return out + quote;
}

function requireNonce(nonce) {
  if (!/^[0-9]{1,19}$/.test(nonce)) {
    throw new CliError(`nonce must be 1-19 ASCII digits, got ${pyRepr(nonce)}`);
  }
}

/** Pull `--seed VALUE` out of argv wherever it appears (before or after
 * the subcommand, matching the Python script's argparse behavior), and
 * return { seed, rest }. A `--seed` with nothing after it (or immediately
 * followed by another flag) is rejected rather than silently treated as
 * "no seed given" — argparse does the same (exit 2, "expected one argument").
 */
function extractSeed(argv) {
  const rest = [];
  let seed;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--seed') {
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        throw new CliError('argument --seed: expected one argument');
      }
      seed = next;
      i++;
    } else {
      rest.push(argv[i]);
    }
  }
  return { seed, rest };
}

function usage() {
  return [
    'usage:',
    '  node sign.js keygen',
    '  node sign.js did   [--seed HEX|PASSPHRASE]',
    '  node sign.js say   [--seed ...] <room> <nonce> <text>',
    '  node sign.js set   [--seed ...] <ns> <key> <nonce> <value>',
  ].join('\n');
}

/** argparse rejects extra positional arguments outright (exit 2,
 * "unrecognized arguments"); without this, destructuring more values
 * than a command expects would silently ignore the surplus instead of
 * refusing it. Not exact-wording-compatible with argparse (not worth
 * chasing), but the non-zero exit and the refusal are what matter.
 */
function expectArity(args, n) {
  if (args.length !== n) {
    throw new CliError(usage());
  }
}

function main() {
  const { seed, rest } = extractSeed(process.argv.slice(2));
  const [cmd, ...args] = rest;

  if (cmd === 'keygen') {
    expectArity(args, 0);
    const seedBytes = crypto.randomBytes(32);
    const key = privateKeyFromSeed(seedBytes);
    console.log(`seed: ${seedBytes.toString('hex')}`);
    console.log(`did:  ${didOf(key)}`);
    return;
  }

  if (cmd === 'did') {
    expectArity(args, 0);
    const key = loadKey(seed);
    console.log(didOf(key));
    return;
  }

  if (cmd === 'say') {
    expectArity(args, 3);
    const [room, nonce, text] = args;
    requireNonce(nonce);
    const canonical = `${room}|${nonce}|${swept(text, MAX_TEXT_CHARS)}`;
    const key = loadKey(seed);
    console.log(didOf(key));
    console.log(signature(key, canonical));
    return;
  }

  if (cmd === 'set') {
    expectArity(args, 4);
    const [ns, key_, nonce, value] = args;
    requireNonce(nonce);
    const canonical = `${ns}|${key_}|${nonce}|${swept(value, MAX_VALUE_CHARS)}`;
    const key = loadKey(seed);
    console.log(didOf(key));
    console.log(signature(key, canonical));
    return;
  }

  throw new CliError(usage());
}

try {
  main();
} catch (err) {
  if (err instanceof CliError) {
    console.error(err.message);
    process.exit(1);
  }
  throw err;
}
