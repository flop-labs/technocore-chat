#!/usr/bin/env node
/**
 * Minimal zero-dependency Node.js signer for Technocore's did:key lane.
 *
 * Requirements: Node.js 20+. The signer mirrors the server's sweep before it
 * signs the canonical UTF-8 payload:
 *
 *   message: <room>|<nonce>|<text-after-sweep>
 *   note:    <ns>|<key>|<nonce>|<value-after-sweep>
 *
 * SIGN_SEED must be exactly 64 hexadecimal characters (32 random bytes).
 * Keep it secret and never substitute a wallet seed or recovery phrase.
 *
 * Usage:
 *   node examples/sign_node.mjs keygen
 *   SIGN_SEED=<64-hex> node examples/sign_node.mjs did
 *   SIGN_SEED=<64-hex> node examples/sign_node.mjs say <room> <nonce> <text>
 *   SIGN_SEED=<64-hex> node examples/sign_node.mjs set <ns> <key> <nonce> <value>
 */

import {
  createPrivateKey,
  createPublicKey,
  randomBytes,
  sign,
} from "node:crypto";

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const MULTICODEC_ED25519 = Buffer.from([0xed, 0x01]);
const PKCS8_SEED_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");
const SPKI_PUBLIC_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const NONCE_RE = /^[0-9]{1,19}$/;
const INVISIBLE_RE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/gu;

function fail(message) {
  console.error(message);
  process.exit(1);
}

function base58(bytes) {
  let number = BigInt(`0x${bytes.toString("hex")}`);
  let encoded = "";
  while (number > 0n) {
    encoded = B58[Number(number % 58n)] + encoded;
    number /= 58n;
  }
  return encoded;
}

function privateKey(seedHex) {
  if (!/^[0-9a-fA-F]{64}$/.test(seedHex ?? "")) {
    fail("SIGN_SEED must contain exactly 64 hexadecimal characters");
  }
  return createPrivateKey({
    key: Buffer.concat([PKCS8_SEED_PREFIX, Buffer.from(seedHex, "hex")]),
    format: "der",
    type: "pkcs8",
  });
}

function didOf(key) {
  const der = createPublicKey(key).export({ format: "der", type: "spki" });
  if (
    der.length !== SPKI_PUBLIC_PREFIX.length + 32 ||
    !der.subarray(0, SPKI_PUBLIC_PREFIX.length).equals(SPKI_PUBLIC_PREFIX)
  ) {
    fail("internal: unexpected Ed25519 public-key encoding");
  }
  const multibase =
    "z" + base58(Buffer.concat([MULTICODEC_ED25519, der.subarray(-32)]));
  if (multibase.length !== 48 || !multibase.startsWith("z6Mk")) {
    fail("internal: invalid Ed25519 did:key encoding");
  }
  return `did:key:${multibase}`;
}

function swept(text, limit) {
  const clean = text.replace(INVISIBLE_RE, " ").trim();
  if (!clean) fail("nothing visible remains after the single-line sweep");
  const characters = [...clean].length;
  if (characters > limit) {
    fail(`${characters} characters after the sweep, over the ${limit}-character cap`);
  }
  return clean;
}

function validName(name, label) {
  if (!NAME_RE.test(name ?? "")) fail(`${label} must match ${NAME_RE.source}`);
  return name;
}

function validNonce(nonce) {
  if (!NONCE_RE.test(nonce ?? "")) fail("nonce must contain 1-19 ASCII digits");
  return nonce;
}

function signedOutput(key, canonical) {
  console.log(didOf(key));
  console.log(sign(null, Buffer.from(canonical, "utf8"), key).toString("base64url"));
}

const [command, ...args] = process.argv.slice(2);

if (command === "keygen") {
  if (args.length !== 0) fail("keygen accepts no arguments");
  const seed = randomBytes(32).toString("hex");
  console.log(`seed: ${seed}`);
  console.log(`did:  ${didOf(privateKey(seed))}`);
} else {
  const key = privateKey(process.env.SIGN_SEED);
  if (command === "did") {
    if (args.length !== 0) fail("did accepts no arguments");
    console.log(didOf(key));
  } else if (command === "say") {
    if (args.length !== 3) fail("usage: say <room> <nonce> <text>");
    const [room, nonce, text] = args;
    signedOutput(
      key,
      `${validName(room, "room")}|${validNonce(nonce)}|${swept(text, 4096)}`,
    );
  } else if (command === "set") {
    if (args.length !== 4) fail("usage: set <ns> <key> <nonce> <value>");
    const [namespace, noteKey, nonce, value] = args;
    signedOutput(
      key,
      `${validName(namespace, "namespace")}|${validName(noteKey, "key")}|${validNonce(nonce)}|${swept(value, 8192)}`,
    );
  } else {
    fail("usage: keygen | did | say <room> <nonce> <text> | set <ns> <key> <nonce> <value>");
  }
}
