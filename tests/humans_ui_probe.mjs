/**
 * Drive /humans in a real browser and report what it actually does.
 *
 * Not a pytest module and not in CI: it needs Chromium, and adding a browser to a service
 * whose test suite is pure-Python with three pinned dependencies is a bigger decision than
 * this page warrants. The Python tests assert what the *served bytes* contain — that the
 * script never builds an anchor, never assigns innerHTML, that the copy label and the icon
 * templates are there. Those are the security invariants and they belong in CI. What they
 * cannot tell you is whether the page works: every one of them passes with the JavaScript
 * completely broken. That is what this is for.
 *
 *     npm i playwright && npx playwright install chromium
 *     CHAT_ROOT=/tmp/ui-store uv run uvicorn app:app --app-dir src --port 8099
 *     node tests/humans_ui_probe.mjs 8099
 *
 * Set CHROMIUM_PATH to reuse a Chromium you already have instead of downloading one.
 *
 * Exits non-zero on the first failed check, so it is usable as a manual gate before
 * shipping a change to the page.
 *
 * Checked 2026-08-19, 24 checks, all passing — expected shape:
 *   desktop 900px   5 columns, copy icon is an <svg> with an accessible name
 *   copy            writes the #r/<room> permalink, swaps glyph + label, restores after 1.2s
 *   filter          narrows rows, counts against LOADED rooms, survives the 5s refresh
 *   open a room     scrolls the Room heading into view
 *   Enter in filter opens the top match
 *   mobile 390px    4 columns (byte column dropped), no horizontal scroll at 320-1280px
 */

import { chromium } from "playwright";

const PORT = process.argv[2] || "8099";
const BASE = `http://127.0.0.1:${PORT}`;
let failures = 0;

function check(label, ok, detail = "") {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail ? "  — " + detail : ""}`);
  if (!ok) failures++;
}

// Playwright normally manages its own Chromium. Where one is already installed — a distro
// package, a CI image that pre-bakes it — point CHROMIUM_PATH at it rather than downloading
// a second copy; empty means "use whatever Playwright installed".
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);

// ---------------------------------------------------------------- seed a store worth reading
{
  const page = await browser.newPage();
  for (const [room, topic] of [
    ["lobby", "the front door for new agents"],
    ["build-notes", "CI failures and release chatter"],
    ["standup", ""],
  ]) {
    await page.goto(`${BASE}/r/${room}/say/seeder/hello%20from%20${room}`);
    if (topic) await page.goto(`${BASE}/kv/topic/${room}/set/${encodeURIComponent(topic)}`);
  }
  await page.close();
}

// ---------------------------------------------------------------------------------- desktop
{
  const context = await browser.newContext({
    viewport: { width: 900, height: 1200 },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${BASE}/humans`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  console.log("desktop 900px");
  const heads = await page.locator("#rooms thead th:visible").allInnerTexts();
  check("column headers", heads.length === 5, heads.join(" / "));

  const share = page.locator("#rooms tbody tr").first().locator("td:last-child button");
  check("copy control is an svg icon", (await share.locator("svg").count()) === 1);
  const name = await share.locator(".sr-only").textContent();
  check("icon carries an accessible name", /^copy link to /.test(name), JSON.stringify(name));

  await share.click();
  await page.waitForTimeout(150);
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  check("copies a #r/<room> permalink", clip.includes("/humans#r/"), clip);
  check("label announces success", (await share.locator(".sr-only").textContent()) === "copied");
  check("glyph changes shape too", (await share.getAttribute("class")).includes("ok"));
  await page.waitForTimeout(1400);
  check("restores after the flash", (await share.locator(".sr-only").textContent()) === name);

  console.log("filter");
  await page.fill("#filter", "release");
  await page.waitForTimeout(150);
  const stats = await page.locator("#stats").innerText();
  check("counts against LOADED rooms", stats.includes("loaded rooms match"), stats.split("·")[0]);
  check("narrows the table", (await page.locator("#rooms tbody tr").count()) === 1);

  await page.fill("#filter", "definitely-no-such-room");
  await page.waitForTimeout(150);
  check(
    "empty state explains itself",
    (await page.locator("#rooms tbody").innerText()).toLowerCase().includes("no room"),
  );

  await page.fill("#filter", "lobby");
  await page.waitForTimeout(6000); // outlast one 5s auto-refresh
  check("filter survives the refresh", (await page.inputValue("#filter")) === "lobby");
  check("and stays applied", (await page.locator("#rooms tbody tr").count()) === 1);

  console.log("navigation");
  await page.fill("#filter", "");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator("#rooms tbody tr").nth(1).locator(".btn-ghost").click();
  await page.waitForTimeout(1200);
  const top = await page.evaluate(() =>
    Math.round(document.getElementById("room-heading").getBoundingClientRect().top),
  );
  check("clicking a room scrolls to it", top >= -5 && top < 300, `heading at y=${top}`);

  await page.evaluate(() => window.scrollTo(0, 0));
  await page.fill("#filter", "standup");
  await page.press("#filter", "Enter");
  await page.waitForTimeout(1000);
  check("Enter opens the top match", (await page.inputValue("#room")) === "standup");

  check("no page errors", errors.length === 0, errors.join("; "));
  await context.close();
}

// ----------------------------------------------------------------------------------- mobile
{
  const context = await browser.newContext({
    viewport: { width: 390, height: 850 },
    isMobile: true,
    hasTouch: true,
  });
  const page = await context.newPage();
  await page.goto(`${BASE}/humans`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);

  console.log("mobile 390px");
  const heads = await page.locator("#rooms thead th:visible").allInnerTexts();
  check("byte column dropped", heads.length === 4, heads.join(" / "));
  const tap = await page.evaluate(() => {
    const b = document.querySelector("td.name .btn-ghost");
    const r = b.getBoundingClientRect();
    return { h: Math.round(r.height), row: Math.round(b.closest("tr").getBoundingClientRect().height) };
  });
  check("room name fills its row as a tap target", tap.h >= tap.row - 14, `${tap.h}px in ${tap.row}px`);
  await context.close();
}

// -------------------------------------------------------- no horizontal scroll, any width
{
  console.log("horizontal overflow");
  for (const width of [320, 390, 560, 700, 900, 1280]) {
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    const page = await context.newPage();
    await page.goto(`${BASE}/humans`, { waitUntil: "networkidle" });
    await page.waitForTimeout(500);
    const r = await page.evaluate(() => ({
      body: document.body.scrollWidth,
      win: window.innerWidth,
    }));
    check(`${width}px`, r.body <= r.win, `body ${r.body} vs viewport ${r.win}`);
    await context.close();
  }
}

await browser.close();
console.log(failures ? `\n${failures} check(s) FAILED` : "\nall checks passed");
process.exit(failures ? 1 : 0);
