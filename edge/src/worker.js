/**
 * Edge policy for the document surface: two lanes, chosen by whether a document's bytes
 * depend on the running configuration.
 *
 *   static-first  /skill.md, /patterns.md, /robots.txt
 *                 Served from the stored copy without asking the origin. Nothing in them
 *                 comes from config — enforced by tests/edge/, which renders each under two
 *                 different configs and requires identical bytes.
 *
 *   origin-first  everything else
 *                 Proxied to the origin; the stored copy is served only when the origin
 *                 fails to answer at all. /llms.txt carries MAX_ROOMS and MAX_NOTES_PER_NS,
 *                 /openapi.json and /.well-known/agent.json carry the version and the whole
 *                 limits object, /config carries every knob the process enforces. A stored
 *                 copy of those served in preference to the origin publishes stale limits
 *                 the moment an operator changes a knob — and knobs get changed during
 *                 incidents, which is when these get read. Measured 2026-09-01: three
 *                 compose knobs changed on the box that day, none of them via a release.
 *
 * Why the split rather than origin-first for everything: origin-first already survives an
 * outage, so the static lane is not about the origin being *down* — it is about the origin
 * being *slow*. That outage spent hours degraded rather than dead, and origin-first waits
 * out its timeout before falling back, so the three documents a reader most needs in order
 * to back off and retry would each have cost a multi-second stall.
 *
 * The cost, accepted: the static three change on a release, so a stored copy is stale until
 * deploy.sh runs again. A release is a controlled moment; a compose edit is not.
 *
 * Not in front of /r/ or /kv/. Those are ~80% of traffic, are writes as often as reads, and
 * none of it is fallback-able — see the per-path routes in wrangler.jsonc.
 */

import ROUTING from "./routing.json";

// How long to wait for the origin on the proxied lane before serving the stored copy.
// Generous: a slow document is still the correct document, and the snapshot is a worse
// answer than a late one.
const ORIGIN_TIMEOUT_MS = 8000;
// The revalidating lane gets a far longer budget: nobody is waiting on a background refresh,
// and what it covers is queueing at a saturated origin rather than work (bench/rooms.py), so
// it has no useful upper bound. Cutting it at the request timeout would mean the copy never
// refreshes on a loaded box, which is exactly when it matters.
const ORIGIN_REVALIDATE_MS = 120000;
// How long the edge may hold a copy — far longer than any refresh interval, deliberately.
// The lane serves the copy however old it is, so an expiry reachable during an origin outage
// would drop it exactly when nothing can replace it, putting the next reader back on the
// walk. Staleness is bounded by refreshes succeeding, not by this.
const EDGE_HOLD_SECONDS = 86400;

const STATIC_FIRST = new Set(ROUTING.static_first);

// Paths the edge owns outright — the origin serves nothing at them, so these stored bytes
// are not a copy of a live answer, they are the only answer. See EDGE_ONLY in snapshot.py.
const EDGE_ONLY = new Set(ROUTING.edge_only ?? []);
// They change only when a deploy changes them, and nothing about them is per-caller, so a
// browser may hold one outright. That is the opposite of every document lane here, where
// max-age=0 is load-bearing because the origin is the authority and the edge is not.
const EDGE_ONLY_MAX_AGE = 86400;

// Paths the edge holds for a few seconds and never stores a snapshot of; see snapshot.py,
// which owns the policy so the Worker and the tests cannot disagree about it. A liveness
// endpoint must never have a stored copy to fall back on — the only thing a stored "ok" can
// do is answer for a service that is gone — so this lane caches and never falls back.
//
// The origin keeps sending `no-store`, and that stays correct: anything asking it directly,
// including the container healthcheck and the auto-updater's rollback probe (both on
// 127.0.0.1, neither through this Worker), gets an uncached answer. Only the edge shares it.
const EDGE_CACHED_SECONDS = ROUTING.edge_cached ?? {};

// Paths served from the edge copy ALWAYS, refreshed behind ctx.waitUntil(). See snapshot.py,
// which owns the policy. The stale copy is the answer; the refresh is a side effect nobody
// waits for. `x-edge-stamp` carries when the copy was made, because the Cache API gives no
// age of its own and `Age` is the CDN's, not this lane's.
const EDGE_REVALIDATE_SECONDS = ROUTING.edge_revalidate ?? {};
const STAMP = "x-edge-stamp";

// The cache-key policy, from snapshot.py. A lane path with no spec goes straight to the
// origin: keying on the raw URL instead is the bug the spec exists to prevent. Fail closed.
const EDGE_KEY = ROUTING.edge_key ?? {};

// One fill per cache key per isolate, cold path included. Isolates are per-PoP, so this is
// not a global lock and does not need to be: what it stops is a burst arriving on one PoP
// with no copy or an expired one, each reader starting its own walk at the origin.
const inFlight = new Map();

/** The key a copy is stored under: the reply space, not the URL space. Parameters the origin
 * ignores are dropped so they cannot multiply entries. Null means "no shared copy for this
 * request" — never a guess. */
function cacheKey(url, pathname) {
  const spec = EDGE_KEY[pathname];
  if (!spec) return null;
  const keep = new URLSearchParams();
  for (const [name, wanted] of Object.entries(spec.match ?? {})) {
    if (url.searchParams.get(name) === wanted) keep.set(name, wanted);
  }
  for (const [name, rule] of Object.entries(spec.clamped ?? {})) {
    const raw = url.searchParams.get(name);
    // Absent means the origin's default: one extra entry, not unboundedly many.
    if (raw === null) continue;
    // Only the form both languages read identically. Python's int() also takes underscores,
    // signs, whitespace and non-ASCII digits, and a disagreement there would not be a miss
    // but a wrong answer — one caller's row count served to another.
    if (!/^[0-9]{1,9}$/.test(raw)) return null;
    keep.set(name, String(Math.min(Number(raw) || rule.min, rule.max)));
  }
  keep.sort();
  const query = keep.toString();
  return new Request(url.origin + pathname + (query ? "?" + query : ""), { method: "GET" });
}

/** A 5xx is the origin failing to answer. A 4xx is the origin answering "no", which is a
 * real reply and must pass through untouched — serving a snapshot over a 404 or a 429 would
 * invent content for a path the service deliberately refused. */
const isOriginFailure = (status) => status >= 500;

/** The stored copy, with the Content-Type the origin gave it when it was captured.
 *
 * The type comes from the manifest rather than the asset server's guess because six of
 * these paths carry no file extension (/humans, /config, /.well-known/api-catalog among
 * them), and a guess hands a browser HTML labelled text/plain or JSON as octet-stream.
 */
async function stored(request, env, pathname, { fallback }) {
  const asset = await env.ASSETS.fetch(new URL(request.url));
  if (!asset || asset.status !== 200) return null;

  const headers = new Headers(asset.headers);
  const recorded = ROUTING.types[pathname];
  if (recorded) headers.set("Content-Type", recorded);
  if (fallback) {
    // Say so, in a header a reader can check and an operator can grep the edge logs for. A
    // snapshot indistinguishable from the live document is how a stale limit gets believed.
    headers.set("X-Origin-Fallback", "1");
    // Short: this copy is only correct until the origin returns, and the shared cache must
    // not hold an outage artefact once the outage is over.
    headers.set("Cache-Control", "public, max-age=0, s-maxage=30, stale-while-revalidate=30");
  }
  return new Response(asset.body, { status: 200, headers });
}

async function edgeCached(request, pathname, seconds) {
  const cache = caches.default;
  const hit = await cache.match(request);
  if (hit) return hit;

  let fresh;
  try {
    fresh = await fetch(request, { signal: AbortSignal.timeout(ORIGIN_TIMEOUT_MS) });
  } catch (err) {
    // An origin that will not answer inside the budget IS the health answer, so report it
    // here rather than letting the timeout escape to the fail-open handler. That handler
    // retries with no deadline of its own, which on a stalled origin would double the work
    // during the outage this endpoint exists to report, and would delay the report until
    // the client gave up instead of ending it at our own deadline.
    return new Response("origin unavailable\n", {
      status: 503,
      headers: { "Cache-Control": "no-store" },
    });
  }

  // Only a healthy answer is worth holding. A 503 from the concurrency limiter is the
  // service saying it is saturated *right now*, and caching that would keep reporting a
  // recovered service as down.
  if (fresh.status === 200) {
    const body = await fresh.arrayBuffer();
    const headers = new Headers(fresh.headers);
    // `max-age=0` is the load-bearing half, exactly as it is in the app's own
    // _edge_cacheable: `s-maxage` is a shared-cache directive, so only Cloudflare holds
    // this copy. A bare `max-age` would let a browser, a monitoring client or a downstream
    // proxy reuse `ok` without contacting the edge at all — liveness staleness outside
    // Cloudflare's control, and beyond the reach of a purge.
    headers.set("Cache-Control", `public, max-age=0, s-maxage=${seconds}`);
    await cache.put(request, new Response(body, { status: 200, headers }));
    return new Response(body, { status: 200, headers });
  }
  return fresh;
}

/** Resolves to the parts of a reply rather than to a Response, because one fill is shared by
 * every reader waiting on it and a Response body can only be consumed once. */
async function fromOrigin(request, key) {
  // Always a GET of the canonical URL, never the caller's own request: route() admits HEAD
  // and the key is a GET, so fetching the request would store a HEAD's empty body under it
  // and every later GET would read an empty /rooms until the copy was replaced. Fetching the
  // key also makes the stored body provably the reply that key names. The caller's headers
  // ride along for the origin's rate accounting; nothing caller-specific is stored, because
  // the guard below refuses any reply that carries some.
  const canonical = new Request(key.url, { method: "GET", headers: request.headers });
  const fresh = await fetch(canonical, { signal: AbortSignal.timeout(ORIGIN_REVALIDATE_MS) });
  const body = await fresh.arrayBuffer();
  const headers = new Headers(fresh.headers);
  if (fresh.status !== 200) return { status: fresh.status, body, headers };

  // A no-store reply is one caller's: /rooms carries a budget footer once a caller's read
  // allowance runs low, and the handler keeps it out of any shared cache. Rewriting the
  // directive would publish that caller's pacing to every reader of this key.
  if (/(^|,)\s*(no-store|private)\b/i.test(headers.get("Cache-Control") ?? "")) {
    return { status: 200, body, headers };
  }
  headers.set(STAMP, String(Date.now()));
  // The edge copy outlives the origin's own short window on purpose: this lane decides when
  // to refresh, so a header that expires sooner would just hand the decision back.
  headers.set("Cache-Control", `public, max-age=0, s-maxage=${EDGE_HOLD_SECONDS}`);
  await caches.default.put(key, new Response(body, { status: 200, headers }));
  return { status: 200, body, headers };
}

/** One origin walk per key, however many readers are waiting on it. */
function fill(request, key) {
  const pending = inFlight.get(key.url);
  if (pending) return pending;
  const job = fromOrigin(request, key).finally(() => inFlight.delete(key.url));
  inFlight.set(key.url, job);
  return job;
}

const asResponse = (r) => new Response(r.body, { status: r.status, headers: r.headers });

async function revalidating(request, ctx, pathname, seconds) {
  const key = cacheKey(new URL(request.url), pathname);
  // No canonical key: serve from the origin without touching the shared copy.
  if (!key) return fetch(request, { signal: AbortSignal.timeout(ORIGIN_REVALIDATE_MS) });

  const hit = await caches.default.match(key);
  // Cold: someone has to be first, but only one of them — the rest join that fill.
  if (!hit) return asResponse(await fill(request, key));

  const stamp = Number(hit.headers.get(STAMP) || 0);
  if (Date.now() - stamp > seconds * 1000) {
    ctx.waitUntil(fill(request, key).catch(() => {}));  // a failed refresh keeps this copy
  }
  return hit;  // always, however old — waiting for this walk is the thing being removed
}

export default {
  async fetch(request, env, ctx) {
    try {
      return await route(request, env, ctx);
    } catch (err) {
      // Fail open. This Worker sits in front of the liveness endpoint now, so an exception
      // in it must not become the service looking dead: hand the request to the origin and
      // let the origin answer for itself.
      return fetch(request);
    }
  },
};

async function route(request, env, ctx) {
    // Only GET/HEAD are ever routed here, but a stray method must not be answered from a
    // stored copy: fall through to the origin and let it refuse.
    if (request.method !== "GET" && request.method !== "HEAD") return fetch(request);

    const pathname = new URL(request.url).pathname;

    // GET only: the Cache API rejects a non-GET request, so a HEAD entering this lane
    // would throw on `cache.put` and unwind to the fail-open handler, which fetches the
    // origin a second time — two origin requests for the one path this lane exists to
    // keep off the origin, and a swallowed exception hiding it. A HEAD falls through to
    // the origin path below instead, which answers it in one request.
    const cacheFor = EDGE_CACHED_SECONDS[pathname];
    if (cacheFor && request.method === "GET") return edgeCached(request, pathname, cacheFor);

    const revalidateAfter = EDGE_REVALIDATE_SECONDS[pathname];
    if (revalidateAfter) return revalidating(request, ctx, pathname, revalidateAfter);

    if (STATIC_FIRST.has(pathname)) {
      const copy = await stored(request, env, pathname, { fallback: false });
      // No stored copy — an un-snapshotted deploy — is not a reason to 404 a document the
      // origin can still serve.
      if (copy) return copy;
      return fetch(request);
    }

    if (EDGE_ONLY.has(pathname)) {
      const copy = await stored(request, env, pathname, { fallback: false });
      // No proxy on a miss: the origin has nothing here, so asking it would only reproduce
      // the 404 this lane exists to stop, several hundred milliseconds later. A missing
      // asset means an incomplete deploy, and that is worth answering plainly.
      if (!copy) {
        return new Response("not found\n", {
          status: 404,
          headers: { "Cache-Control": "no-store" },
        });
      }
      copy.headers.set("Cache-Control", `public, max-age=${EDGE_ONLY_MAX_AGE}`);
      return copy;
    }

    let originResponse = null;
    try {
      // `fetch(request)` from a Worker on a route goes to the origin, not back into this
      // Worker. The timeout turns a hung tunnel into a fallback rather than a hung request.
      originResponse = await fetch(request, { signal: AbortSignal.timeout(ORIGIN_TIMEOUT_MS) });
      if (!isOriginFailure(originResponse.status)) return originResponse;
    } catch (err) {
      // Timeout, DNS, refused connection, tunnel down: indistinguishable here from a 5xx,
      // and handled the same way.
    }

    const copy = await stored(request, env, pathname, { fallback: true });
    // The origin's own failure is a better answer than a 404 we invented.
    return copy ?? originResponse ?? new Response("origin unavailable\n", { status: 503 });
}
