// Execute the tracked Worker with controlled Fetch streams and a complete stored document.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const [workerPath, mode] = process.argv.slice(2);
const source = readFileSync(workerPath, "utf8").replace(
  'import ROUTING from "./routing.json";',
  'const ROUTING = {static_first: [], types: {"/config": "application/json"}};',
);
const worker = (await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`)).default;
const failed = ["headers-reset", "body-reset", "body-timeout", "missing-copy"].includes(mode);
const fallback = failed && mode !== "missing-copy";
const originalTimeout = AbortSignal.timeout;
let controller;
let calls = 0;
let copies = 0;
let originResponse;
AbortSignal.timeout = (ms) => {
  assert.equal(ms, 8000, "the document lane retains its existing deadline");
  controller = new AbortController();
  return controller.signal;
};
globalThis.fetch = async (_request, options) => {
  calls++;
  assert.ok(options?.signal, "a body failure must not trigger the unbounded fail-open retry");
  if (mode === "headers-reset") throw new TypeError("connection reset");
  const body = failed ? new ReadableStream({
    start(stream) {
      stream.enqueue(new TextEncoder().encode('{"incomplete":'));
      options.signal.addEventListener("abort", () => stream.error(options.signal.reason));
      setTimeout(() => {
        if (mode === "body-timeout") controller.abort(new DOMException("deadline", "TimeoutError"));
        else stream.error(new TypeError("connection reset"));
      }, 0);
    },
  }) : ["head", "not-modified"].includes(mode) ? null : mode === "empty" ? "" : '{"live":true}';
  originResponse = new Response(body, {
    status: mode === "refusal" ? 429 : mode === "not-modified" ? 304 : 200,
    headers: {"Content-Type": "application/json", "Cache-Control": "no-store", ETag: '"live"'},
  });
  return originResponse;
};
try {
  const response = await worker.fetch(new Request(
    `https://example.invalid/${mode === "unlisted" ? "rooms-other" : "config"}`,
    {method: mode === "head" ? "HEAD" : "GET"},
  ), {ASSETS: {async fetch() {
    copies++;
    return new Response('{"stored":true}', {status: mode === "missing-copy" ? 404 : 200});
  }}}, {});
  assert.equal(calls, 1);
  assert.equal(response.status, mode === "missing-copy" ? 503
    : mode === "refusal" ? 429 : mode === "not-modified" ? 304 : 200);
  assert.equal(copies, failed ? 1 : 0);
  assert.equal(response.headers.get("X-Origin-Fallback"), fallback ? "1" : null);
  assert.equal(await response.text(), fallback ? '{"stored":true}' : mode === "missing-copy"
    ? "origin unavailable\n" : ["head", "empty", "not-modified"].includes(mode) ? "" : '{"live":true}');
  if (!failed) {
    assert.equal(response.headers.get("ETag"), '"live"');
    assert.equal(response.headers.get("Cache-Control"), "no-store");
  }
  if (["unlisted", "head", "refusal", "not-modified"].includes(mode)) {
    assert.equal(response, originResponse, "only successful document GETs need buffering");
  }
  console.log(`${mode}: passed`);
} finally {
  AbortSignal.timeout = originalTimeout;
}
