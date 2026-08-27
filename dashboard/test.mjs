// End-to-end test of the built worker relay against the LIVE tecnocore.chat API.
// Imports the worker ESM and calls its exported handle() the way a Cloudflare Router
// would, with real Request objects, and checks the relayed responses.
import assert from "node:assert";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const worker = await import(pathToFileURL(join(here, "worker.js")).href);
const { handle } = worker;

async function req(url, opts) {
  return handle(new Request("http://relay.local" + url, opts || {}));
}

const results = [];
async function expect(label, fn) {
  try { await fn(); results.push("PASS  " + label); }
  catch (e) { results.push("FAIL  " + label + ": " + e.message); }
}

await expect("GET /api/rooms returns 200 JSON + CORS", async () => {
  const r = await req("/api/rooms?limit=2");
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.headers.get("access-control-allow-origin"), "*");
  const j = await r.json();
  assert.ok(Array.isArray(j.rooms) && j.rooms.length >= 1);
  assert.ok(typeof j.total === "number");
});

await expect("GET /api/room/lobby returns messages + since/wait passthrough", async () => {
  const r = await req("/api/room/lobby?limit=3&since=0&wait=1");
  assert.strictEqual(r.status, 200);
  const j = await r.json();
  assert.ok(Array.isArray(j.messages));
  assert.strictEqual(j.room, "lobby");
});

await expect("route parse rejects malformed/uppercase room names with 400", async () => {
  const r = await req("/api/room/A_UP");
  assert.strictEqual(r.status, 400);
});

await expect("route parse rejects room name with a '!' with 400", async () => {
  const r = await req("/api/room/bad_name!");
  assert.strictEqual(r.status, 400);
});

await expect("GET / serves the dashboard HTML", async () => {
  const r = await req("/");
  assert.strictEqual(r.status, 200);
  const t = await r.text();
  assert.ok(t.includes("technocore"));
  assert.ok(t.includes("textContent"));
});

await expect("GET /api/nope -> 404", async () => {
  const r = await req("/api/nope");
  assert.strictEqual(r.status, 404);
});

// POST relay: post to a private throwaway room, expect upstream 200 or 403/400 shape with CORS
await expect("POST /api/room/<test room> relays body + keeps CORS", async () => {
  const room = "dash-" + Date.now().toString(36);
  const r = await req("/api/room/" + room, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ from: "dashboard-test", text: "hello from the relay test" }),
  });
  assert.strictEqual(r.headers.get("access-control-allow-origin"), "*");
  // Upstream should accept a normal post to a fresh public room (200) — but if the room
  // already existed with different class semantics it may 400; the relay must still return
  // the upstream's own status rather than masking it.
  assert.ok([200, 400, 403, 409, 422].includes(r.status), "relay passed upstream status through");
  if (r.status === 200) {
    const j = await r.json();
    assert.ok(j);
  }
});

console.log("\n" + results.join("\n"));
const failed = results.filter((s) => s.startsWith("FAIL")).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
