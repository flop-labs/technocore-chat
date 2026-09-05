// Builds the single-file `worker.js` from `worker.src.js` + `dashboard.html`.
// The dashboard HTML is embedded as a JS string literal (backtick-free, escaped) so the
// worker stays one deployable artifact with zero external reads at runtime.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

function escapeForJsString(s) {
  // Backticks and ${ } would break a template literal; \ and control chars must be escaped.
  return s
    .replace(/\\/g, "\\\\")
    .replace(/`/g, "\\`")
    .replace(/\$\{/g, "\\${")
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n");
}

const src = readFileSync(join(here, "worker.src.js"), "utf8");
const html = readFileSync(join(here, "dashboard.html"), "utf8");
const escaped = escapeForJsString(html);

if (!src.includes("__DASHBOARD_HTML__")) {
  throw new Error("worker.src.js is missing the __DASHBOARD_HTML__ marker");
}
if (!escaped.includes("<html")) {
  throw new Error("escape failed: dashboard HTML did not survive inlining");
}

const out = src.replace("__DASHBOARD_HTML__", "`" + escaped + "`");
writeFileSync(join(here, "worker.js"), out, "utf8");
console.log(`built worker.js (${out.length} bytes, dashboard ${html.length} bytes inlined)`);
