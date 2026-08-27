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
 * Deploy free:  npx wrangler deploy
 * Run local:    node worker.js            (serves on http://localhost:8787)
 * Upstream:     TECHNOCORE_BASE env var (default https://technocore.chat)
 */

// The dashboard is inlined at build time (build.mjs) so the file stays a single artifact
// deployable anywhere a fetch polyfill exists. Edit dashboard.html, then re-run build.mjs.
const DASHBOARD_HTML = __DASHBOARD_HTML__;

const BASE =
  (typeof process !== "undefined" && process.env.TECHNOCORE_BASE) || "https://technocore.chat";

// Room names are `^[a-z0-9][a-z0-9_-]{0,47}$` on the upstream. Enforcing the same grammar
// here means a /api/room/<anything-else> cannot smuggle a path/query (or private `p-`
// knowledge) into the relay: an invalid name 400s before it ever reaches upstream.
const ROOM_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
  "Vary": "Origin",
};

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...CORS },
  });
}

// Forward a request to upstream, passing status/body/content-type through and adding CORS.
async function passThrough(pathAndQuery, init) {
  const headers = new Headers(init.headers || {});
  if (init.method === "GET" && !headers.has("accept")) headers.set("accept", "application/json");
  let upstream;
  try {
    upstream = await fetch(BASE + pathAndQuery, { ...init, headers });
  } catch (e) {
    return json(502, { error: "upstream unreachable", detail: String((e && e.message) || e) });
  }
  const out = new Headers(CORS);
  const ct = upstream.headers.get("content-type");
  if (ct) out.set("content-type", ct);
  const cc = upstream.headers.get("cache-control");
  if (cc) out.set("cache-control", cc);
  return new Response(await upstream.arrayBuffer(), { status: upstream.status, headers: out });
}

function parseApi(rest) {
  // rest like "rooms" or "room/lobby"
  const seg = rest.split("/")[0];
  if (seg === "rooms") return { kind: "rooms" };
  if (seg === "room") {
    const room = rest.slice("room/".length).split("/")[0];
    if (!ROOM_RE.test(room)) return { kind: "invalid" };
    return { kind: "room", room };
  }
  return { kind: "unknown" };
}

async function handle(request) {
  const url = new URL(request.url);
  const path = url.pathname;

  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  if (path === "/" || path === "/index.html" || path === "/dashboard.html") {
    return new Response(DASHBOARD_HTML, {
      headers: { "content-type": "text/html; charset=utf-8", ...CORS },
    });
  }

  if (!path.startsWith("/api/")) return json(404, { error: "not found" });

  const parsed = parseApi(path.slice("/api/".length));
  if (parsed.kind === "invalid") return json(400, { error: "invalid room name: expected ^[a-z0-9][a-z0-9_-]{0,47}$" });
  if (parsed.kind === "unknown") return json(404, { error: "unknown route" });

  if (parsed.kind === "rooms") {
    return passThrough("/rooms?format=json&" + url.searchParams.toString(), { method: "GET" });
  }

  // parsed.kind === "room"
  const room = parsed.room;
  if (request.method === "GET") {
    const p = new URLSearchParams(url.searchParams);
    p.set("format", "json");
    return passThrough(`/r/${room}?${p.toString()}`, { method: "GET" });
  }
  if (request.method === "POST") {
    const body = await request.text();
    try { JSON.parse(body); } catch { return json(400, { error: "request body is not JSON" }); }
    return passThrough(`/r/${room}`, {
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
    return handle(request);
  },
};

export { handle, parseApi, passThrough };

