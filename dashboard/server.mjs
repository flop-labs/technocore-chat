// Local run:  node server.mjs        (serves http://localhost:8787)
// Runs the same worker ESM as a plain Node HTTP server, so you can preview the dashboard
// and the relay without Cloudflare. The relay needs no CORS at all here (same origin), but
// keeps its headers so the page also works when embedded/opened from another origin.
import http from "node:http";
import { fileURLToPath, pathToFileURL } from "node:url";
import { join } from "node:path";

const here = fileURLToPath(new URL(".", import.meta.url));
const worker = await import(pathToFileURL(join(here, "worker.js")).href);
const { handle } = worker;

const server = http.createServer((req, res) => {
  const bodyPromise =
    req.method === "GET" || req.method === "HEAD"
      ? Promise.resolve(undefined)
      : new Promise((ok) => {
          const chunks = [];
          req.on("data", (c) => chunks.push(c));
          req.on("end", () => ok(Buffer.concat(chunks)));
        });
  bodyPromise
    .then((body) =>
      handle(
        new Request("http://relay.local" + (req.url || "/"), {
          method: req.method,
          headers: req.headers,
          body,
        })
      )
    )
    .then(async (r) => {
      res.writeHead(r.status, Object.fromEntries(r.headers.entries()));
      res.end(Buffer.from(await r.arrayBuffer()));
    })
    .catch((e) => {
      res.writeHead(500, { "content-type": "text/plain" });
      res.end(String((e && e.stack) || e));
    });
});

const port = Number(process.env.PORT) || 8787;
server.listen(port, () => {
  console.log(`technocore-live relay on http://localhost:${port}`);
});
