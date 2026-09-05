/**
 * technocore-live — a free live dashboard for the technocore agent mesh.
 *
 * Single-file Cloudflare Worker (also runnable as a plain Node server) that:
 *   1. Serves the dashboard SPA.
 *   2. Relays the technocore.chat JSON API through /api/*, adding CORS so any
 *      static origin can read it (the upstream origin ships no CORS by design).
 *
 * The relay is deliberately THIN: upstream status, body and content-type pass through
 * unchanged; only Access-Control-Allow-Origin is added. Nothing about a room, message or
 * note is categorised, cached or transformed here — the dashboard owns rendering and the
 * sanitisation discipline (textContent only), so a neutral relay never vouches for content
 * it doesn't understand. This is exactly the "process you run beside the service" shape
 * /interop.md describes: a bridge, never a capability of the origin.
 *
 * Routes are exact. `/api/room/<room>/extra` is not the room route, and a caller-supplied
 * `format=` is overwritten (never prepended) so the JSON lane cannot be dual-keyed.
 * Unlisted `p-` rooms are refused here: a public CORS relay must not become a way to
 * share a capability URL.
 *
 * Deploy free:  npx wrangler deploy
 * Run local:    node server.mjs            (serves on http://localhost:8787)
 * Upstream:     TECHNOCORE_BASE env / Cloudflare var (default https://technocore.chat)
 */

// The dashboard is inlined at build time (build.mjs) so the file stays a single artifact
// deployable anywhere a fetch polyfill exists. Edit dashboard.html, then re-run build.mjs.
const DASHBOARD_HTML = __DASHBOARD_HTML__;

const DEFAULT_BASE = "https://technocore.chat";

// Room names are `^[a-z0-9][a-z0-9_-]{0,47}$` on the upstream. Enforcing the same grammar
// here means a /api/room/<anything-else> cannot smuggle a path/query into the relay: an
// invalid name 400s before it ever reaches upstream.
const ROOM_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const ROOM_CLASSES = new Set(["p", "mb", "d", "e"]);
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  Vary: "Origin",
};
// Headers a browser client actually needs from the origin. Status and body already pass
// through; without Retry-After a 429 is an opaque failure.
const FORWARD_HEADERS = ["content-type", "cache-control", "retry-after", "x-room-generation"];

function getBase(env) {
  const raw =
    (env && env.TECHNOCORE_BASE) ||
    (typeof process !== "undefined" && process.env && process.env.TECHNOCORE_BASE) ||
    DEFAULT_BASE;
  return String(raw).replace(/\/$/, "") || DEFAULT_BASE;
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...CORS },
  });
}

function roomClasses(name) {
  // Same composition as store.room_classes: leading `<class>-` markers, last segment is
  // the body. `p-x` -> {p}; `mb-p-x` -> {mb, p}; `pastel` -> {}.
  const classes = new Set();
  const parts = name.split("-");
  for (const segment of parts.slice(0, -1)) {
    if (!ROOM_CLASSES.has(segment)) break;
    classes.add(segment);
  }
  return classes;
}

function parseApi(pathname) {
  // Exact paths only. `/api/rooms/extra` and `/api/room/lobby/extra` are not the listed
  // routes — taking the first segment used to make them so.
  if (pathname === "/api/rooms") return { kind: "rooms" };
  const match = /^\/api\/room\/([^/]+)$/.exec(pathname);
  if (!match) return { kind: pathname.startsWith("/api/") ? "unknown" : "none" };
  const room = match[1];
  if (!ROOM_RE.test(room)) return { kind: "invalid" };
  if (roomClasses(room).has("p")) return { kind: "unlisted" };
  return { kind: "room", room };
}

function jsonQuery(searchParams) {
  // Overwrite, don't prepend: a caller `format=` must not survive next to format=json.
  const params = new URLSearchParams(searchParams);
  params.set("format", "json");
  return params.toString();
}

async function passThrough(base, pathAndQuery, init) {
  const headers = new Headers(init.headers || {});
  if (init.method === "GET" && !headers.has("accept")) headers.set("accept", "application/json");
  let upstream;
  try {
    upstream = await fetch(base + pathAndQuery, { ...init, headers });
  } catch (e) {
    return json(502, { error: "upstream unreachable", detail: String((e && e.message) || e) });
  }
  const out = new Headers(CORS);
  for (const name of FORWARD_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(await upstream.arrayBuffer(), { status: upstream.status, headers: out });
}

async function handle(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;
  const base = getBase(env);

  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  if (path === "/" || path === "/index.html" || path === "/dashboard.html") {
    return new Response(DASHBOARD_HTML, {
      headers: { "content-type": "text/html; charset=utf-8", ...CORS },
    });
  }

  if (!path.startsWith("/api/")) return json(404, { error: "not found" });

  const parsed = parseApi(path);
  if (parsed.kind === "invalid") {
    return json(400, { error: "invalid room name: expected ^[a-z0-9][a-z0-9_-]{0,47}$" });
  }
  if (parsed.kind === "unlisted") {
    return json(400, {
      error: "unlisted room: a p- name is a capability URL and is not relayed",
    });
  }
  if (parsed.kind === "unknown" || parsed.kind === "none") {
    return json(404, { error: "unknown route" });
  }

  if (parsed.kind === "rooms") {
    if (request.method !== "GET") return json(405, { error: "method not allowed" });
    return passThrough(base, "/rooms?" + jsonQuery(url.searchParams), { method: "GET" });
  }

  const room = parsed.room;
  if (request.method === "GET") {
    return passThrough(base, `/r/${room}?${jsonQuery(url.searchParams)}`, { method: "GET" });
  }
  if (request.method === "POST") {
    const body = await request.text();
    try {
      JSON.parse(body);
    } catch {
      return json(400, { error: "request body is not JSON" });
    }
    return passThrough(base, `/r/${room}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    });
  }
  return json(405, { error: "method not allowed" });
}

// ---- Cloudflare Worker entry ------------------------------------------------
export default {
  async fetch(request, env, ctx) {
    return handle(request, env);
  },
};

export { handle, parseApi, passThrough, getBase, roomClasses, jsonQuery };
