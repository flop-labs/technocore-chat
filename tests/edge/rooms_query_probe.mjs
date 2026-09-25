/** Execute the tracked Worker with local fetch/cache doubles and report its /rooms key. */

import fs from "node:fs";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
let source = fs.readFileSync(input.worker, "utf8");
const routingImport = 'import ROUTING from "./routing.json";';
const defaultExport = "export default {";

if (!source.includes(routingImport) || !source.includes(defaultExport)) {
  throw new Error("worker module shape changed; update the edge query probe");
}
source = source.replace(routingImport, `const ROUTING = ${JSON.stringify(input.routing)};`);
source = source.replace(defaultExport, "globalThis.__edgeWorker = {");

const originRequests = [];
const cacheWrites = [];
globalThis.caches = {
  default: {
    match: async () => null,
    put: async (request) => cacheWrites.push(request.url),
  },
};
globalThis.fetch = async (request) => {
  originRequests.push(typeof request === "string" ? request : request.url);
  return new Response("mock origin\n", {
    status: 200,
    headers: { "Cache-Control": "public" },
  });
};

// The two substitutions above provide the generated deployment manifest locally and expose
// the Worker's ordinary default export. All route and cache-key code executed below is the
// tracked production source.
eval(source);

const results = [];
for (const url of input.urls) {
  originRequests.length = 0;
  cacheWrites.length = 0;
  await globalThis.__edgeWorker.fetch(
    new Request(url),
    { ASSETS: { fetch: async () => new Response("not found\n", { status: 404 }) } },
    { waitUntil() {} },
  );
  results.push({
    request: url,
    cacheKey: cacheWrites[0],
    canonicalRequest: originRequests[0],
  });
}

process.stdout.write(JSON.stringify(results));
