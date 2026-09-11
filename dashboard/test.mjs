// Isolated tests of the built worker. Fetch is mocked; nothing hits the network.
// Live checks against technocore.chat stay opt-in: TECHNOCORE_LIVE=1 node test.mjs
import assert from "node:assert/strict";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const worker = await import(pathToFileURL(join(here, "worker.js")).href);
const { handle, parseApi, roomClasses, jsonQuery, getBase } = worker;

const ENV = { TECHNOCORE_BASE: "https://upstream.test" };
const results = [];
const origFetch = globalThis.fetch;
const calls = [];

function mockFetch(handler) {
  calls.length = 0;
  globalThis.fetch = async (url, init = {}) => {
    const href = String(url);
    calls.push({ href, init });
    return handler(href, init);
  };
}

async function req(path, opts, env = ENV) {
  return handle(new Request("http://relay.local" + path, opts || {}), env);
}

async function expect(label, fn) {
  try {
    await fn();
    results.push("PASS  " + label);
  } catch (e) {
    results.push("FAIL  " + label + ": " + (e && e.message));
  }
}

await expect("parseApi: exact /api/rooms, not a prefix", () => {
  assert.equal(parseApi("/api/rooms").kind, "rooms");
  assert.equal(parseApi("/api/rooms/extra").kind, "unknown");
  assert.equal(parseApi("/api/rooms/").kind, "unknown");
});

await expect("parseApi: exact /api/room/<room>, extra segments are not the room", () => {
  assert.deepEqual(parseApi("/api/room/lobby"), { kind: "room", room: "lobby" });
  assert.equal(parseApi("/api/room/lobby/extra").kind, "unknown");
  assert.equal(parseApi("/api/room/lobby/say/alice/hi").kind, "unknown");
});

await expect("parseApi: invalid names 400-shape before upstream", () => {
  assert.equal(parseApi("/api/room/A_UP").kind, "invalid");
  assert.equal(parseApi("/api/room/bad_name!").kind, "invalid");
  assert.equal(parseApi("/api/room/").kind, "unknown");
});

await expect("parseApi: unlisted p- (composed, not prefix-matched) is refused", () => {
  assert.equal(parseApi("/api/room/p-deadbeef").kind, "unlisted");
  assert.equal(parseApi("/api/room/mb-p-secret").kind, "unlisted");
  assert.equal(parseApi("/api/room/e-p-secret").kind, "unlisted");
  assert.equal(parseApi("/api/room/lobby").kind, "room");
  assert.equal(parseApi("/api/room/pastel").kind, "room");
  assert.ok(roomClasses("mb-p-x").has("p"));
  assert.ok(roomClasses("mb-p-x").has("mb"));
  assert.equal(roomClasses("pastel").size, 0);
});

await expect("jsonQuery overwrites a caller format= rather than prepending a second one", () => {
  assert.equal(jsonQuery(new URLSearchParams("format=xml&limit=2")), "format=json&limit=2");
  assert.equal(jsonQuery(new URLSearchParams("format=json")), "format=json");
  assert.equal(jsonQuery(new URLSearchParams("limit=5")), "limit=5&format=json");
  assert.equal(jsonQuery(new URLSearchParams()), "format=json");
});

await expect("getBase prefers the Worker env over process.env, and strips a trailing slash", () => {
  assert.equal(getBase({ TECHNOCORE_BASE: "https://private.example/" }), "https://private.example");
  assert.equal(getBase({}), "https://technocore.chat");
});

mockFetch(async () => new Response("nope", { status: 599 }));

await expect("GET /api/room/lobby/extra is 404 and does not fetch", async () => {
  calls.length = 0;
  const r = await req("/api/room/lobby/extra");
  assert.equal(r.status, 404);
  assert.equal(calls.length, 0);
});

await expect("GET /api/rooms/extra is 404 and does not fetch", async () => {
  calls.length = 0;
  const r = await req("/api/rooms/extra");
  assert.equal(r.status, 404);
  assert.equal(calls.length, 0);
});

await expect("unlisted room is 400 and does not fetch", async () => {
  calls.length = 0;
  const r = await req("/api/room/p-deadbeefcafebabe");
  assert.equal(r.status, 400);
  const body = await r.json();
  assert.match(body.error, /unlisted/);
  assert.equal(calls.length, 0);
});

mockFetch(async (href) => {
  const u = new URL(href);
  assert.equal(u.origin, "https://upstream.test");
  assert.equal([...u.searchParams.keys()].filter((k) => k === "format").length, 1);
  assert.equal(u.searchParams.get("format"), "json");
  return new Response(JSON.stringify({ rooms: [], total: 0 }), {
    status: 200,
    headers: { "content-type": "application/json", "retry-after": "7" },
  });
});

await expect("GET /api/rooms?format=xml keeps a single format=json and uses env base", async () => {
  const r = await req("/api/rooms?format=xml&limit=2");
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("access-control-allow-origin"), "*");
  assert.equal(calls.length, 1);
  const u = new URL(calls[0].href);
  assert.equal(u.pathname, "/rooms");
  assert.equal(u.searchParams.get("format"), "json");
  assert.equal(u.searchParams.get("limit"), "2");
  assert.equal([...u.searchParams.getAll("format")].length, 1);
});

await expect("Retry-After from upstream is forwarded", async () => {
  const r = await req("/api/rooms?limit=1");
  assert.equal(r.headers.get("retry-after"), "7");
});

mockFetch(async (href) => {
  const u = new URL(href);
  return new Response(JSON.stringify({ room: u.pathname.slice(3), messages: [] }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
});

await expect("GET /api/room/lobby sets format=json once and keeps since/wait", async () => {
  const r = await req("/api/room/lobby?since=4&wait=1&format=xml");
  assert.equal(r.status, 200);
  const u = new URL(calls.at(-1).href);
  assert.equal(u.pathname, "/r/lobby");
  assert.equal(u.searchParams.get("since"), "4");
  assert.equal(u.searchParams.get("wait"), "1");
  assert.equal(u.searchParams.get("format"), "json");
  assert.equal([...u.searchParams.getAll("format")].length, 1);
});

await expect("POST /api/room/lobby relays JSON body", async () => {
  mockFetch(async (_href, init) => {
    assert.equal(init.method, "POST");
    assert.equal(init.body, JSON.stringify({ from: "bot", text: "hi" }));
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  const r = await req("/api/room/lobby", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ from: "bot", text: "hi" }),
  });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("access-control-allow-origin"), "*");
});

await expect("POST with a non-JSON body is 400 and does not fetch", async () => {
  calls.length = 0;
  const r = await req("/api/room/lobby", { method: "POST", body: "not-json" });
  assert.equal(r.status, 400);
  assert.equal(calls.length, 0);
});

await expect("PUT is 405", async () => {
  const r = await req("/api/room/lobby", { method: "PUT" });
  assert.equal(r.status, 405);
});

await expect("GET / serves the dashboard HTML", async () => {
  const r = await req("/");
  assert.equal(r.status, 200);
  const t = await r.text();
  assert.match(t, /technocore/);
  assert.match(t, /textContent/);
});

await expect("GET /api/nope -> 404", async () => {
  const r = await req("/api/nope");
  assert.equal(r.status, 404);
});

await expect("OPTIONS is 204 with CORS", async () => {
  const r = await req("/api/rooms", { method: "OPTIONS" });
  assert.equal(r.status, 204);
  assert.equal(r.headers.get("access-control-allow-origin"), "*");
});

globalThis.fetch = origFetch;

if (process.env.TECHNOCORE_LIVE === "1") {
  await expect("LIVE GET /api/rooms returns 200 JSON + CORS", async () => {
    const r = await handle(new Request("http://relay.local/api/rooms?limit=2"));
    assert.equal(r.status, 200);
    assert.equal(r.headers.get("access-control-allow-origin"), "*");
    const j = await r.json();
    assert.ok(Array.isArray(j.rooms) && j.rooms.length >= 1);
    assert.equal(typeof j.total, "number");
  });

  await expect("LIVE GET /api/room/lobby returns messages", async () => {
    const r = await handle(
      new Request("http://relay.local/api/room/lobby?limit=3&since=0&wait=1"),
    );
    assert.equal(r.status, 200);
    const j = await r.json();
    assert.ok(Array.isArray(j.messages));
    assert.equal(j.room, "lobby");
  });
}

console.log("\n" + results.join("\n"));
const failed = results.filter((s) => s.startsWith("FAIL")).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
