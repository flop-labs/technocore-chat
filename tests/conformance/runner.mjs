/**
 * Run `vectors.json` against a JavaScript implementation of the signed lane.
 *
 * Two things in one file, deliberately:
 *
 *   1. A REFERENCE implementation (`reference`) that passes every vector. It is here to be
 *      read and copied, because "the sweep is six Unicode categories" is a sentence that
 *      four different people have now turned into four different pieces of code. Roughly
 *      forty lines, no dependencies outside `node:crypto`.
 *
 *   2. A RUNNER that checks any implementation against the vectors, including the
 *      `naive` one — which is kept, and expected to FAIL, because it is the mistake this
 *      whole file exists to catch. A pitfall documented in prose is a pitfall; a pitfall
 *      with a failing test beside it is a lesson.
 *
 *     node tests/conformance/runner.mjs                 # reference + naive
 *     node tests/conformance/runner.mjs --only reference
 *     node tests/conformance/runner.mjs --module ./my-client.mjs   # your own
 *
 * A --module must export `sweep(text) -> string`, and may also export
 * `didKeyFromPublicKey(bytes) -> string` and `fingerprint(did) -> string`; whatever is
 * missing is skipped rather than failed.
 *
 * Why the vectors carry code points and not strings: the swept set includes `Cs`, and a lone
 * surrogate has no UTF-8 encoding. `JSON.parse` does recover `"\ud800"` — the escape is legal
 * and the file is written ensure_ascii=True — but re-encoding that string to UTF-8, or a parser
 * that folds unpaired surrogates to U+FFFD, would substitute a different character. Every case
 * is rebuilt here with String.fromCodePoint(...case.in_cp), which cannot drift that way.
 */

import { readFileSync } from "node:fs";
import { createHash, verify as nodeVerify, createPublicKey, createPrivateKey } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join, resolve } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const vectors = JSON.parse(readFileSync(join(HERE, "vectors.json"), "utf8"));

// ---------------------------------------------------------------- reference implementation

/**
 * The sweep: every character in Cc, Cf, Cs, Co, Zl or Zp becomes a space, then trim.
 *
 * `/u` is not decoration. Without it the regex matches UTF-16 CODE UNITS, so every astral
 * character — every emoji above U+FFFF — is seen as two characters of category Cs and
 * replaced with two spaces. The signature then covers text the server never computes, and
 * the write comes back 403 with no indication why. `/u` makes the engine iterate code
 * points, which is what Python's `for c in text` does.
 *
 * `.trim()` is part of the transform, not cosmetic: the server's `clean_text` ends in
 * `.strip()`, so the trimmed form is what gets hashed. Sweep first and trim second — a
 * character swept to a space at either end has to disappear, and trimming first leaves it.
 */
const SWEEP_RE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/gu;

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export const reference = {
  name: "reference (code points, /u)",
  sweep: (text) => text.replace(SWEEP_RE, " ").trim(),

  didKeyFromPublicKey(pub) {
    // multicodec ed25519-pub (0xed 0x01) + the 32 key bytes, base58btc, 'z' multibase tag.
    const bytes = Uint8Array.from([0xed, 0x01, ...pub]);
    let n = 0n;
    for (const b of bytes) n = (n << 8n) | BigInt(b);
    let out = "";
    while (n > 0n) {
      out = B58[Number(n % 58n)] + out;
      n /= 58n;
    }
    for (const b of bytes) {         // leading zero bytes are '1's; never happens for
      if (b !== 0) break;            // ed25519-pub, since the codec starts 0xed — but an
      out = B58[0] + out;            // encoder that omits it is wrong for other inputs
    }
    return "did:key:z" + out;
  },

  fingerprint: (did) => createHash("sha256").update(did, "utf8").digest("hex").slice(0, 16),

  /** `<room>|<nonce>|<swept text>` as UTF-8 — seq and ts are the server's and not covered. */
  payload(room, nonce, text) {
    return Buffer.from(`${room}|${nonce}|${this.sweep(text)}`, "utf8");
  },
};

// ------------------------------------------------------------------ the mistake, as a test

export const naive = {
  name: "naive (UTF-16 code units) — EXPECTED TO FAIL",
  expectFail: true,
  // `split('')` splits on code units. So does `for (let i = 0; i < s.length; i++)`, and so
  // does any regex without /u. All three break every astral character in half.
  sweep: (text) =>
    text
      .split("")
      .map((c) => (/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/u.test(c) ? " " : c))
      .join("")
      .trim(),
};

// ------------------------------------------------------------------------------- the runner

const cps = (s) => Array.from(s).map((c) => c.codePointAt(0));
const hex = (a) => "[" + a.map((c) => c.toString(16)).join(" ") + "]";

function checkSweep(impl, report) {
  if (typeof impl.sweep !== "function") return report.skip("sweep", "not exported");
  for (const c of vectors.sweep_cases) {
    const input = String.fromCodePoint(...c.in_cp);
    const want = String.fromCodePoint(...c.out_cp);
    let got;
    try {
      got = impl.sweep(input);
    } catch (e) {
      report.fail("sweep", c.name, `threw ${e.message}`);
      continue;
    }
    if (got === want) report.pass("sweep", c.name);
    else
      report.fail(
        "sweep",
        c.name,
        `got ${hex(cps(got))} want ${hex(cps(want))}`,
        c.version_sensitive ? "version-sensitive: check your runtime's Unicode tables" : c.note,
      );
  }
}

function checkDid(impl, report) {
  if (typeof impl.didKeyFromPublicKey !== "function")
    return report.skip("did:key", "not exported");
  for (const id of vectors.identities) {
    // Derive the public key from the seed the same way any Ed25519 library must: the vector
    // carries the seed so this does not depend on the vector's own DID being right.
    const pub = ed25519PublicFromSeed(Buffer.from(id.seed_hex, "hex"));
    const got = impl.didKeyFromPublicKey(pub);
    if (got === id.did) report.pass("did:key", id.did.slice(0, 20) + "…");
    else report.fail("did:key", id.seed_hex.slice(0, 8), `got ${got} want ${id.did}`);
  }
}

function checkFingerprint(impl, report) {
  if (typeof impl.fingerprint !== "function") return report.skip("fingerprint", "not exported");
  for (const id of vectors.identities) {
    const got = impl.fingerprint(id.did);
    if (got === id.fingerprint) report.pass("fingerprint", id.fingerprint);
    else report.fail("fingerprint", id.did.slice(0, 20), `got ${got} want ${id.fingerprint}`);
  }
}

function checkPayload(impl, report) {
  if (typeof impl.payload !== "function") return report.skip("payload", "not exported");
  for (const c of vectors.signature_cases) {
    const raw = String.fromCodePoint(...c.text_raw_cp);
    const got = Buffer.from(impl.payload(c.room, c.nonce, raw)).toString("hex");
    if (got === c.payload_utf8_hex) report.pass("payload", c.name);
    else report.fail("payload", c.name, `got ${got}\n        want ${c.payload_utf8_hex}`, c.note);
  }
}

/** Every one of the sixteen spellings of each signature must verify (issue #177). */
function checkSignatureSpellings() {
  let passed = 0;
  const failures = [];
  for (const c of vectors.signature_cases) {
    const spki = Buffer.concat([
      Buffer.from("302a300506032b6570032100", "hex"), // Ed25519 SubjectPublicKeyInfo prefix
      ed25519PublicFromSeed(Buffer.from(c.seed_hex, "hex")),
    ]);
    const key = createPublicKey({ key: spki, format: "der", type: "spki" });
    const msg = Buffer.from(c.payload_utf8_hex, "hex");
    let ok = 0;
    for (const s of c.sig_accepted_spellings) {
      if (nodeVerify(null, msg, key, Buffer.from(s + "==", "base64url"))) ok++;
    }
    const n = c.sig_accepted_spellings.length;
    if (ok === n) passed++;
    else failures.push(`${c.name}: only ${ok}/${n} of the accepted spellings verified`);
  }
  console.log(
    `signature spellings: ${passed}/${vectors.signature_cases.length} cases had all ` +
      `${vectors.signature_cases[0].sig_accepted_spellings.length} spellings verify`,
  );
  console.log(
    "  a signature is 64 bytes in 86 base64url characters — 516 bits for 512, so the last 4\n" +
      "  bits are free and every signature has sixteen valid encodings. The server accepts all\n" +
      "  of them, so two conformant clients can emit different `sig` strings for one message.\n" +
      "  Anything that compares or caches signatures as strings has to decode them first.",
  );
  for (const f of failures) console.log(`  FAIL ${f}`);
  return failures.length > 0;
}

/** Ed25519 public key from a 32-byte seed: wrap the seed as PKCS#8 and let node derive it. */
function ed25519PublicFromSeed(seed) {
  const pkcs8 = Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]);
  const priv = createPrivateKey({ key: pkcs8, format: "der", type: "pkcs8" });
  const der = createPublicKey(priv).export({ format: "der", type: "spki" });
  return der.subarray(der.length - 32); // strip the 12-byte SPKI header
}

// ------------------------------------------------------------------------------------ main

function makeReport(label) {
  const r = { label, passed: 0, failed: 0, skipped: [], failures: [] };
  r.pass = () => r.passed++;
  r.fail = (group, name, detail, note) => {
    r.failed++;
    r.failures.push({ group, name, detail, note });
  };
  r.skip = (group, why) => r.skipped.push(`${group} (${why})`);
  return r;
}

function run(impl) {
  const report = makeReport(impl.name);
  checkSweep(impl, report);
  checkDid(impl, report);
  checkFingerprint(impl, report);
  checkPayload(impl, report);

  const verdict = impl.expectFail
    ? report.failed > 0
      ? "as documented — this is the trap"
      : "!! the naive implementation PASSED, which means the vectors lost their teeth"
    : report.failed === 0
      ? "conformant"
      : "NOT conformant";
  console.log(`\n${impl.name}`);
  console.log(`  ${report.passed} passed, ${report.failed} failed  —  ${verdict}`);
  if (report.skipped.length) console.log(`  skipped: ${report.skipped.join(", ")}`);
  for (const f of report.failures) {
    console.log(`  FAIL ${f.group}/${f.name}`);
    console.log(`       ${f.detail}`);
    if (f.note) console.log(`       why it matters: ${f.note}`);
  }
  return impl.expectFail ? report.failed === 0 : report.failed > 0;
}

const args = process.argv.slice(2);
const only = args.includes("--only") ? args[args.indexOf("--only") + 1] : null;
const modulePath = args.includes("--module") ? args[args.indexOf("--module") + 1] : null;

console.log(
  `vectors: unicode ${vectors.provenance.unicode_version} (generated) vs ` +
    `${process.versions.unicode ?? "unknown"} (this node ${process.version})`,
);
console.log(
  `  ${vectors.sweep_cases.length} sweep · ${vectors.identities.length} identities · ` +
    `${vectors.signature_cases.length} signature cases`,
);

let bad = false;
let impls;
if (modulePath) {
  // pathToFileURL, not the bare path: a dynamic import of "C:\..." is rejected as an
  // unsupported URL scheme ("c:"), so passing --module a Windows absolute path fails.
  const ns = await import(pathToFileURL(resolve(modulePath)).href);
  // Accept either shape: a module that exports sweep/payload/... directly, or an adapter
  // file exporting one object per client — testing several clients in one run is the point.
  impls =
    typeof ns.sweep === "function"
      ? [{ name: modulePath, ...ns }]
      : Object.entries(ns)
          .filter(([, v]) => v && typeof v === "object" && typeof v.sweep === "function")
          .map(([key, v]) => ({ name: v.name ?? key, ...v }));
  if (!impls.length) {
    console.error(`\n${modulePath} exports no sweep(text) function, and no object with one.`);
    process.exit(2);
  }
} else {
  impls = [reference, naive].filter((i) => !only || i.name.startsWith(only));
}
for (const impl of impls) bad = run(impl) || bad;

console.log("");
bad = checkSignatureSpellings() || bad;

process.exit(bad ? 1 : 0);
