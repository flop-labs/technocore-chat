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
// The revalidating lane gets its own, far longer budget: it is a background refresh nobody
// is waiting on, and what it covers is queueing at a saturated origin rather than work
// (bench/rooms.py), which has no useful upper bound. Cutting it at the request timeout would
// mean the copy never refreshes on a loaded box, which is exactly when it matters.
const ORIGIN_REVALIDATE_MS = 120000;
// How long the edge may hold a copy. Longer than the refresh interval by design: the lane,
// not the header, decides freshness, and an expiry shorter than the refresh would drop the
// copy that makes the whole thing non-blocking.
const EDGE_HOLD_SECONDS = 3600;

const STATIC_FIRST = new Set(ROUTING.static_first);

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

// One refresh per path per isolate. Workers isolates are per-PoP and short-lived, so this is
// not a global lock — it does not need to be. What it prevents is the case that actually
// bites: a burst of readers arriving on one PoP against an expired copy, each queueing its
// own walk at an origin whose thread pool is the thing being protected.
const inFlight = new Map();

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

async function fromOrigin(request, cache, pathname) {
  const fresh = await fetch(request, { signal: AbortSignal.timeout(ORIGIN_REVALIDATE_MS) });
  if (fresh.status !== 200) return fresh;
  const body = await fresh.arrayBuffer();
  const headers = new Headers(fresh.headers);
  headers.set(STAMP, String(Date.now()));
  // The edge copy outlives the origin's own short window on purpose: this lane decides when
  // to refresh, so a header that expires sooner would just hand the decision back.
  headers.set("Cache-Control", `public, max-age=0, s-maxage=${EDGE_HOLD_SECONDS}`);
  await cache.put(request, new Response(body, { status: 200, headers }));
  return new Response(body, { status: 200, headers });
}

async function revalidating(request, ctx, pathname, seconds) {
  const cache = caches.default;
  const hit = await cache.match(request);

  if (!hit) return fromOrigin(request, cache, pathname);  // cold: someone has to be first

  const stamp = Number(hit.headers.get(STAMP) || 0);
  if (Date.now() - stamp > seconds * 1000 && !inFlight.has(pathname)) {
    const job = fromOrigin(request, cache, pathname)
      .catch(() => {})                      // a failed refresh keeps the copy we have
      .finally(() => inFlight.delete(pathname));
    inFlight.set(pathname, job);
    ctx.waitUntil(job);
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

    const cacheFor = EDGE_CACHED_SECONDS[pathname];
    if (cacheFor) return edgeCached(request, pathname, cacheFor);

    const revalidateAfter = EDGE_REVALIDATE_SECONDS[pathname];
    if (revalidateAfter) return revalidating(request, ctx, pathname, revalidateAfter);

    if (STATIC_FIRST.has(pathname)) {
      const copy = await stored(request, env, pathname, { fallback: false });
      // No stored copy — an un-snapshotted deploy — is not a reason to 404 a document the
      // origin can still serve.
      if (copy) return copy;
      return fetch(request);
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
