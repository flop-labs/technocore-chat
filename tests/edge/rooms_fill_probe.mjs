// Run the actual Worker with only its platform APIs replaced. No network or npm packages.
// Usage: node rooms_fill_probe.mjs <scenario> <worker_path> <routing_json>
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const [scenario, workerPath, routingJson] = process.argv.slice(2);
const cases = {
  "cold-429": { status: 429, policy: "no-store" },
  "cold-no-store": { status: 200, policy: "no-store" },
  "cold-private": { status: 200, policy: "private" },
  "cold-public": { status: 200, policy: "public, max-age=0, s-maxage=5" },
  "cold-private-followers": { status: 200, policy: "no-store", privateFollower: true },
  "warm-429": { status: 429, policy: "no-store", warm: true },
  "warm-no-store": { status: 200, policy: "no-store", warm: true },
  "warm-public": { status: 200, policy: "public, max-age=0, s-maxage=5", warm: true },
};
assert.ok(Object.hasOwn(cases, scenario), `unknown scenario: ${scenario}`);
const options = cases[scenario];
const routing = JSON.parse(routingJson);
const importLine = 'import ROUTING from "./routing.json";';
const source = readFileSync(workerPath, "utf8");
assert.equal(source.split(importLine).length, 2, "replace exactly the routing import");
const moduleSource = source.replace(importLine, `const ROUTING = ${JSON.stringify(routing)};`);
const { default: worker } = await import(
  `data:text/javascript;base64,${Buffer.from(moduleSource).toString("base64")}`
);

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function probe() {
  const firstOrigin = deferred();
  const secondMatch = deferred();
  const originGate = deferred();
  const originCalls = [];
  const cacheWrites = [];
  const background = [];
  const callerIPs = { A: "192.0.2.1", B: "192.0.2.2" };
  const canonicalURL = "https://example.invalid/rooms?limit=2";
  const publicReply = options.policy.startsWith("public");
  const leaderBody = options.status === 429
    ? "429 retry after: 30s (caller A)\n"
    : publicReply ? "public room listing\n" : "room listing\n# budget: caller A has 1 read left\n";
  const followerBody = options.privateFollower
    ? "room listing\n# budget: caller B has 2 reads left\n"
    : "caller B room listing, budget available\n";
  const staleBody = "previously cached room listing\n";
  let matches = 0;

  globalThis.caches = {
    default: {
      async match(key) {
        assert.equal(key.method, "GET");
        assert.equal(key.url, canonicalURL);
        matches += 1;
        if (matches === 2) secondMatch.resolve();
        // Both callers reach the same cold or stale entry before the origin can finish.
        return options.warm ? new Response(staleBody, {
          headers: { "x-edge-stamp": "1", "Cache-Control": "public, max-age=0, s-maxage=86400" },
        }) : undefined;
      },
      async put(key, response) {
        assert.equal(key.method, "GET");
        assert.equal(key.url, canonicalURL);
        cacheWrites.push({
          status: response.status,
          body: await response.text(),
          policy: response.headers.get("Cache-Control"),
          stamp: response.headers.get("x-edge-stamp"),
        });
      },
    },
  };
  globalThis.fetch = async (request, init) => {
    const caller = request.headers.get("X-Probe-Caller");
    assert.ok(Object.hasOwn(callerIPs, caller), "origin retains the initiating caller's headers");
    assert.equal(request.headers.get("CF-Connecting-IP"), callerIPs[caller]);
    assert.equal(request.method, "GET");
    assert.equal(request.url, canonicalURL, "origin receives the canonical representation URL");
    assert.ok(init?.signal instanceof AbortSignal, "origin fetch retains its deadline");
    originCalls.push(caller);
    firstOrigin.resolve();
    await originGate.promise;
    const leader = caller === "A";
    return new Response(leader ? leaderBody : followerBody, {
      status: leader ? options.status : 200,
      headers: {
        "Cache-Control": leader ? options.policy
          : options.privateFollower ? "no-store" : "public, max-age=0, s-maxage=5",
      },
    });
  };

  const request = (caller) => worker.fetch(new Request(
    "https://example.invalid/rooms?ignored=probe&limit=2",
    { headers: { "CF-Connecting-IP": callerIPs[caller], "X-Probe-Caller": caller } },
  ), {}, { waitUntil(job) { background.push(job); } });
  const a = request("A");
  await firstOrigin.promise;
  const b = request("B");
  await secondMatch.promise;
  // Cache.match has resolved for B. Drain its continuation before releasing A's origin
  // response, so B observes the same pending fill without relying on a timing delay.
  await new Promise((done) => setImmediate(done));

  let replies;
  if (options.warm) {
    // A warm response must finish while the background origin request is still blocked.
    replies = await Promise.all([a, b]);
    assert.equal(originCalls.length, 1, "stale readers join a single background refresh");
  }
  originGate.resolve();
  replies ??= await Promise.all([a, b]);
  await Promise.all(background);
  const bodies = await Promise.all(replies.map((response) => response.text()));
  assert.equal(matches, 2);

  if (options.warm) {
    assert.deepEqual(replies.map((response) => response.status), [200, 200]);
    assert.deepEqual(bodies, [staleBody, staleBody]);
    assert.deepEqual(originCalls, ["A"], "private refresh results cause no follower fetch");
    assert.equal(background.length, 2, "both stale readers schedule the shared refresh");
  } else {
    assert.equal(replies[0].status, options.status, "leader keeps its own status");
    assert.equal(bodies[0], leaderBody, "leader keeps its own response");
    assert.equal(replies[1].status, 200, "follower must not inherit another caller's refusal");
    assert.equal(bodies[1], publicReply ? leaderBody : followerBody,
      "only public replies can be shared with another caller");
    assert.deepEqual(originCalls, publicReply ? ["A"] : ["A", "B"]);
    assert.equal(background.length, 0);
  }

  const expectedWrites = publicReply ? [leaderBody]
    : options.warm || options.privateFollower ? [] : [followerBody];
  assert.deepEqual(cacheWrites.map((write) => write.body), expectedWrites,
    "only public responses enter the shared cache");
  for (const write of cacheWrites) {
    assert.equal(write.status, 200);
    assert.match(write.policy, /^public, max-age=0, s-maxage=\d+$/);
    assert.ok(Number(write.stamp) > 0, "successful fills record their refresh timestamp");
  }
}

let timer;
try {
  await Promise.race([
    probe(),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${scenario}: probe timed out`)), 5000);
    }),
  ]);
  console.log(`${scenario}: passed`);
} finally {
  clearTimeout(timer);
}
