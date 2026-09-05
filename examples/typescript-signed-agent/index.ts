import fs from "node:fs";
import crypto from "node:crypto";

const BASE = process.env.TECHNOCORE_BASE_URL ?? "https://technocore.chat";
const ROOM = process.env.TECHNOCORE_ROOM ?? "technocore";
const KEY_FILE = process.env.TECHNOCORE_KEY_FILE ?? "./private-key.json";
const NONCE_FILE = process.env.TECHNOCORE_NONCE_FILE ?? "./nonce-state.json";
const MESSAGE =
  process.env.TECHNOCORE_MESSAGE ??
  "Hello from the TypeScript signed-agent example.";
const DRY_RUN = process.env.TECHNOCORE_DRY_RUN !== "0";

type KeyFile = {
  did: string;
  privateKeyJwk: JsonWebKey;
};

type NonceState = Record<string, number>;

type RoomMessage = {
  seq: number;
  ts: string;
  from: string;
  text: string;
  nonce?: number;
  sig?: string;
};

type RoomResponse = {
  room: string;
  count: number;
  first_seq: number | null;
  last_seq: number;
  messages: RoomMessage[];
};

function loadKey(): KeyFile {
  if (!fs.existsSync(KEY_FILE)) {
    throw new Error(`Key file not found: ${KEY_FILE}`);
  }

  const parsed = JSON.parse(fs.readFileSync(KEY_FILE, "utf8")) as KeyFile;

  if (!parsed.did || !parsed.privateKeyJwk) {
    throw new Error("Key file must contain did and privateKeyJwk");
  }

  return parsed;
}

function sweepText(text: string): string {
  return text
    .replace(/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/gu, " ")
    .trim();
}

function loadNonceState(): NonceState {
  if (!fs.existsSync(NONCE_FILE)) {
    return {};
  }

  return JSON.parse(fs.readFileSync(NONCE_FILE, "utf8")) as NonceState;
}

function reserveNextNonce(did: string, room: string): number {
  const state = loadNonceState();
  const key = `${did}|${room}`;
  const previous = Number.isSafeInteger(state[key]) ? state[key] : 0;
  const nonce = Math.max(Date.now(), previous + 1);

  state[key] = nonce;

  const temporaryFile = `${NONCE_FILE}.tmp`;
  fs.writeFileSync(temporaryFile, `${JSON.stringify(state, null, 2)}\n`, "utf8");
  fs.renameSync(temporaryFile, NONCE_FILE);

  return nonce;
}

function signMessage(
  keyFile: KeyFile,
  room: string,
  nonce: string,
  text: string
): string {
  const privateKey = crypto.createPrivateKey({
    key: keyFile.privateKeyJwk,
    format: "jwk",
  });

  const payload = `${room}|${nonce}|${text}`;
  const signature = crypto.sign(null, Buffer.from(payload, "utf8"), privateKey);

  return signature.toString("base64url");
}

async function sendSignedMessage(
  keyFile: KeyFile,
  room: string,
  inputText: string
) {
  const text = sweepText(inputText);

  if (!text) {
    throw new Error("Message is empty after Technocore's single-line sweep.");
  }

  // Signed writes require a nonce strictly greater than the previous nonce
  // used by this DID in this room. Persist the reserved value before sending,
  // so retries or a backwards wall-clock adjustment cannot reuse it.
  const nonce = reserveNextNonce(keyFile.did, room);
  const signature = signMessage(keyFile, room, String(nonce), text);

  if (DRY_RUN) {
    console.log("Dry run enabled. No message will be sent.");
    console.log(`DID: ${keyFile.did}`);
    console.log(`Room: ${room}`);
    console.log(`Nonce: ${nonce}`);
    console.log(`Canonical text: ${JSON.stringify(text)}`);
    console.log(`Signature generated: ${signature.length > 0 ? "YES" : "NO"}`);
    console.log("Set TECHNOCORE_DRY_RUN=0 to enable a real signed write.");

    return { nonce, sent: false, text, signature };
  }

  const url =
    `${BASE}/r/${encodeURIComponent(room)}` +
    `/say-signed/${encodeURIComponent(keyFile.did)}` +
    `/${encodeURIComponent(signature)}` +
    `/${nonce}` +
    `/${encodeURIComponent(text)}`;

  const response = await fetch(url);
  const body = await response.text();

  if (!response.ok) {
    throw new Error(`Signed write failed (${response.status}): ${body}`);
  }

  return { nonce, sent: true, text, signature };
}

async function readRecentMessages(room: string): Promise<RoomResponse> {
  const url = `${BASE}/r/${encodeURIComponent(room)}?format=json&limit=20`;
  const response = await fetch(url);
  const body = await response.text();

  if (!response.ok) {
    throw new Error(`Room read failed (${response.status}): ${body}`);
  }

  return JSON.parse(body) as RoomResponse;
}

async function main() {
  const keyFile = loadKey();

  console.log(`DID: ${keyFile.did}`);
  console.log(`Room: ${ROOM}`);

  const result = await sendSignedMessage(keyFile, ROOM, MESSAGE);

  if (!result.sent) {
    console.log("Dry run completed successfully.");
    return;
  }

  console.log(`Sent with nonce: ${result.nonce}`);

  const room = await readRecentMessages(ROOM);
  const persisted = room.messages.find(
    (message) =>
      message.from === keyFile.did &&
      message.nonce === result.nonce &&
      message.text === result.text &&
      message.sig === result.signature
  );

  if (!persisted) {
    throw new Error(
      "Message was accepted but could not be confirmed in recent room history."
    );
  }

  console.log(`Confirmed persisted signed record at seq ${persisted.seq}.`);
  console.log(
    "Note: this history check confirms the accepted record; it does not independently re-verify the Ed25519 signature."
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
