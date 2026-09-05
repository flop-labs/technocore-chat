/**
 * Drive /humans in a real browser and report what it actually does.
 *
 * Not a pytest module: it needs Chromium. The Python tests assert what the *served bytes*
 * contain — that the script never builds an anchor, never assigns innerHTML, that the copy
 * label and the icon templates are there. Those are the security invariants and they belong
 * in the Python suite. What they cannot tell you is whether the page works: every one of
 * them passes with the JavaScript completely broken. That is what this is for.
 *
 * This ran as a manual gate for as long as /humans was a read-only window, on the reasoning
 * that adding a browser to a service whose suite is pure Python with three pinned
 * dependencies was a bigger decision than the page warranted. The page now holds a signing
 * key, derives one from an authenticator, and mints delegation records — and two P1s on
 * PR #719 were both browser-side and both invisible to 684 passing Python tests. So it runs
 * in CI now, in .github/workflows/humans.yml, on changes to the page and to the four things
 * that can break it from underneath. The Python line is untouched: the dependency is
 * tests/package.json and its lockfile, and `uv sync` never sees it.
 *
 *     cd tests && npm ci && npx playwright install chromium && cd ..
 *     CHAT_ROOT=/tmp/ui-store uv run uvicorn app:app --app-dir src --port 8099
 *     node tests/humans_ui_probe.mjs 8099
 *
 * Set CHROMIUM_PATH to reuse a Chromium you already have instead of downloading one.
 *
 * Exits non-zero on the first failed check, so it is usable by hand before pushing as well
 * as by the workflow.
 *
 * Checked 2026-09-05, 105 checks, all passing — expected shape:
 *   desktop 900px   5 columns, copy icon is an <svg> with an accessible name
 *   copy            writes the #r/<room> permalink, swaps glyph + label, restores after 1.2s
 *   filter          narrows rows, counts against LOADED rooms, survives the 5s refresh
 *   open a room     scrolls the Room heading into view
 *   Enter in filter opens the top match
 *   mobile 390px    4 columns (byte column dropped), no horizontal scroll at 320-1280px
 *   webmcp          eight tools register on load, measured through the browser's own
 *                   getTools()/executeTool() (Chrome 151 + --enable-features=WebMCP, set
 *                   in the launch args; a stub stands in where the flag does nothing),
 *                   hints are right, and every tool actually reaches the server
 *   webmcp absent   the page is unchanged with no modelContext, and with one that throws
 *   live            the log and composer are above the fold and the directory below them,
 *                   a message posted elsewhere arrives in well under the old 5s timer, rows
 *                   carry an age, and a reader up in the history is offered the new messages
 *                   rather than being scrolled away from what they were reading
 *   signing         a pasted seed yields the did:key scripts/sign.py derives for it, the
 *                   server accepts the signature, the nonce steps on a second write, the
 *                   invisible-character sweep matches the server's, the identity survives
 *                   a reload, and signing out lands back on the nickname lane
 *   passkey         a virtual authenticator with PRF enrols, derives a did:key, stores no
 *                   seed, and hands the SAME did:key back to a browser whose storage has
 *                   been wiped; discovery with nothing enrolled refuses instead of enrolling
 *   delegation      two `delegate:` records are signed, published to the DID note path
 *                   beside an existing `mailbox:`, and both read back verified out of the
 *                   ONE line a note can hold; four malformed ones are refused before a fetch
 *
 * The webmcp and signing sections both post messages, which reorders /rooms — they run
 * last, after every check that reads the seeded list.
 *
 * The signing section needs a *secure context* for crypto.subtle, so it works against
 * 127.0.0.1 (which browsers treat as trustworthy) and would report a page with no identity
 * row at all against a LAN address over plain HTTP — correctly, and not because anything
 * is broken.
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
const browser = await chromium.launch({
  // Chrome ships WebMCP behind this flag (151 does; --enable-blink-features=WebMCP and
  // --enable-experimental-web-platform-features turn it on too). With it the WebMCP
  // section below drives the browser's own ModelContext instead of a stub. It changes
  // nothing for any other check.
  args: ["--enable-features=WebMCP"],
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
});

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
  await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });
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
  await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });
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
    await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(500);
    const r = await page.evaluate(() => ({
      body: document.body.scrollWidth,
      win: window.innerWidth,
    }));
    check(`${width}px`, r.body <= r.win, `body ${r.body} vs viewport ${r.win}`);
    await context.close();
  }
}

// ------------------------------------------------------------------------------- WebMCP
// Driven through the browser's own ModelContext where the launch flag above enabled it:
// registration is measured with getTools(), and every tool is called with executeTool(),
// which is exactly the path an agent takes. Where the flag does nothing — an older build,
// Playwright's bundled Chromium — a stub with the same three methods stands in, so the
// section reports either way and the checks below are identical.
//
// Three things the real implementation does that the draft IDL does not say, all measured
// rather than assumed: executeTool takes its input as a JSON *string*, getTools() hands
// inputSchema back as a JSON *string* (the page registers an object; Chrome serialises it
// on the way out), and the callback gets the parsed object with no options bag at all.
// All three are caller-side — the page reads input.room either way, and guard() already
// treats the options argument as optional — but a check that does not parse the schema
// passes vacuously, which is how the first version of this section fooled itself.
{
  // Installed at document start and self-effacing: where Chrome's own ModelContext is
  // present it steps aside, so the checks below run against the real implementation
  // without the probe having to know in advance which one it got. (It cannot be decided
  // by sniffing a blank page first — the API is not exposed on about:blank.)
  const STUB = () => {
    if (navigator.modelContext) return;
    window.__stubbedModelContext = true;
    const tools = [];
    const mc = {
      registerTool(tool, options) {
        if (tools.some((t) => t.name === tool.name)) return Promise.reject(new Error("duplicate"));
        tools.push(tool);
        if (options && options.signal) {
          options.signal.addEventListener("abort", () => {
            const i = tools.indexOf(tool);
            if (i > -1) tools.splice(i, 1);
          });
        }
        return Promise.resolve();
      },
      getTools: () =>
        Promise.resolve(tools.map((t) => Object.assign({}, t, { origin: location.origin }))),
      executeTool: (tool, json) =>
        Promise.resolve(tools.find((t) => t.name === tool.name).execute(JSON.parse(json)))
          .then((r) => JSON.stringify(r)),
    };
    Object.defineProperty(navigator, "modelContext", { value: mc, configurable: true });
  };

  const context = await browser.newContext({ viewport: { width: 900, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.addInitScript(STUB);
  await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(800);

  const native = !(await page.evaluate(() => window.__stubbedModelContext === true));
  console.log(`webmcp (${native ? "Chrome's own ModelContext" : "stubbed — the flag had no effect"})`);
  const tools = await page.evaluate(async () =>
    (await navigator.modelContext.getTools()).map((t) => ({
      name: t.name,
      description: t.description,
      schema: t.inputSchema
        ? (typeof t.inputSchema === "string" ? JSON.parse(t.inputSchema) : t.inputSchema)
        : null,
      annotations: t.annotations || {},
      origin: t.origin,
    })),
  );
  const names = tools.map((t) => t.name).sort();
  check("the browser reports eight tools registered on load",
        JSON.stringify(names) === JSON.stringify(
          ["get_manual", "list_notes", "list_rooms", "open_room", "post_message",
           "read_note", "read_room", "write_note"]), names.join(", "));
  check("each carries a description and an object schema through registration",
        tools.every((t) => t.description.length > 40 && t.schema && t.schema.type === "object"),
        tools.filter((t) => !(t.schema && t.schema.type === "object")).map((t) => t.name).join(","));
  // The arguments a model must supply survive registration too, not just the outer shape.
  check("required arguments survive the round trip",
        tools.find((t) => t.name === "read_room").schema.required.join() === "room" &&
        tools.find((t) => t.name === "write_note").schema.required.join() === "ns,key,value",
        JSON.stringify(tools.find((t) => t.name === "write_note").schema.required));
  check("tools are scoped to this origin", tools.every((t) => t.origin === new URL(BASE).origin),
        tools[0].origin);
  // The hints are the security half of this feature: readOnlyHint tells a model which
  // tools cannot change anything, untrustedContentHint which results a stranger wrote.
  // Getting either wrong is worse than not shipping the tool.
  const named = (key) => tools.filter((t) => t.annotations[key]).map((t) => t.name).sort().join(",");
  check("the five readers are marked readOnlyHint",
        named("readOnlyHint") === "get_manual,list_notes,list_rooms,read_note,read_room",
        named("readOnlyHint"));
  check("every tool returning agent-written text is marked untrustedContentHint",
        named("untrustedContentHint") === "list_notes,list_rooms,post_message,read_note,read_room,write_note",
        named("untrustedContentHint"));

  const exec = (name, input) =>
    page.evaluate(async ([n, i]) => {
      const tool = (await navigator.modelContext.getTools()).find((t) => t.name === n);
      return navigator.modelContext.executeTool(tool, JSON.stringify(i));
    }, [name, input]);
  const call = async (name, input) => JSON.parse(await exec(name, input));

  const rooms = await call("list_rooms", {});
  check("executeTool answers with a JSON string", typeof (await exec("list_rooms", {})) === "string");
  const roomList = JSON.parse(rooms.content[0].text);
  check("list_rooms reached the server", roomList.rooms.some((r) => r.room === "lobby"),
        `${roomList.total} rooms`);
  const filtered = JSON.parse((await call("list_rooms", { filter: "CI failures" })).content[0].text);
  check("list_rooms filters on the topic too",
        filtered.matched === 1 && filtered.rooms[0].room === "build-notes");
  const view = JSON.parse((await call("read_room", { room: "lobby" })).content[0].text);
  check("read_room returns the room's messages", view.messages.length >= 1,
        `${view.messages.length} messages`);

  const posted = await call("post_message", { room: "lobby", from: "webmcp", text: "posted by a tool" });
  check("post_message writes and echoes the stored message",
        !posted.isError && JSON.parse(posted.content[0].text).posted.text === "posted by a tool",
        posted.content[0].text.slice(0, 60));
  await page.waitForTimeout(700);          // poll() is a fetch; the tool result does not wait on it
  check("the page the reader is looking at refreshed itself",
        (await page.locator("#log .msg").filter({ hasText: "posted by a tool" }).count()) >= 1);

  await call("write_note", { ns: "plans", key: "probe", value: "ship it" });
  const note = await call("read_note", { ns: "plans", key: "probe" });
  // Exactly the stored value, not the server's framing. /kv/<ns>/<key> answers with the
  // untrusted-content banner and a blank line ahead of the value, and a tool that forwards
  // that is not returning what it advertises.
  check("read_note returns exactly the value that was stored",
        note.content[0].text === "ship it", JSON.stringify(note.content[0].text));

  // The loop the manual documents, driven end to end: read a note, write it back guarded
  // by what you read. It only terminates if read_note's output is byte-identical to the
  // stored value, so this is the check that a banner leaking into the result would fail.
  const rebased = await call("write_note",
    { ns: "plans", key: "probe", value: "shipped", if: note.content[0].text });
  check("its output feeds write_note's `if` and the compare-and-swap wins",
        !rebased.isError, rebased.content[0].text.split("\n")[0]);

  const stale = await call("write_note", { ns: "plans", key: "probe", value: "no", if: "wrong" });
  check("write_note refuses a stale compare-and-swap", stale.isError === true,
        stale.content[0].text.split("\n")[0]);
  // And the 409 keeps the value that is actually there, which lives *after* the first line
  // — the whole point of the conditional-write response is rebasing without re-reading.
  check("a lost compare-and-swap carries the current value back",
        stale.content[0].text.trim().endsWith("shipped"),
        JSON.stringify(stale.content[0].text.slice(-40)));
  check("list_notes lists the key",
        (await call("list_notes", { ns: "plans" })).content[0].text.includes("probe"));
  check("get_manual returns /llms.txt",
        (await call("get_manual", {})).content[0].text.includes("/r/<room>/say/"));

  // A refusal has to come back as a result the model can read, not as an exception it
  // cannot see. Chrome does not validate against the advertised schema before calling —
  // a missing required argument arrives as undefined — so allowed() is load-bearing, not
  // a nicety, and the three cases below all have to land in the same shape.
  const bad = await call("read_room", { room: "Not A Room!" });
  check("a bad argument comes back as isError, not a rejection",
        bad.isError === true && bad.content[0].text.includes("must match"), bad.content[0].text);
  const omitted = await call("read_room", {});
  check("a missing required argument never reads a room", omitted.isError === true,
        omitted.content[0].text);
  const missing = await call("read_note", { ns: "plans", key: "nothing-here" });
  check("a 404 comes back as the server's own sentence",
        missing.isError === true && missing.content[0].text.startsWith("404"));

  await page.evaluate(() => window.scrollTo(0, 0));
  const opened = await call("open_room", { room: "build-notes" });
  await page.waitForTimeout(1200);
  check("open_room moves this page to the room",
        !opened.isError && (await page.evaluate(() => location.hash)) === "#r/build-notes",
        opened.content[0].text);

  // Teardown, driven through the real listeners rather than asserted from the source.
  // One AbortController owns all eight, so aborting it is what unregisters them — and the
  // persisted/non-persisted split is the whole logic: a document parked in the back/forward
  // cache is alive but off screen and should not be offering open_room, while one that is
  // really unloading takes its tools with it and needs no help.
  const registered = () => page.evaluate(async () => (await navigator.modelContext.getTools()).length);
  const fire = (type, persisted) =>
    page.evaluate(([t, p]) => window.dispatchEvent(new PageTransitionEvent(t, { persisted: p })),
                  [type, persisted]).then(() => page.waitForTimeout(150));

  await fire("pagehide", true);
  check("the abort signal withdraws all eight at once", (await registered()) === 0,
        `${await registered()} left`);
  await fire("pageshow", true);
  check("a reader who comes back gets them again", (await registered()) === 8);
  await fire("pagehide", false);
  check("a real unload withdraws nothing — the document takes them with it",
        (await registered()) === 8);
  check("and the tools still work after the round trip",
        (JSON.parse(await exec("list_rooms", {}))).isError === false);

  check("no page errors", errors.length === 0, errors.join("; "));
  await context.close();
}

// ------------------------------------------------------ the page without a modelContext
// The registration block is last and wrapped for one reason: a browser that does not have
// this API, or half-has it, must still get the page a person came for. Both cases here.
{
  console.log("webmcp absent or broken");
  for (const [label, init] of [
    ["no modelContext at all", null],
    ["a registerTool that throws", () => {
      Object.defineProperty(navigator, "modelContext", {
        value: { registerTool() { throw new TypeError("half-implemented"); } },
        configurable: true,
      });
    }],
  ]) {
    const context = await browser.newContext({ viewport: { width: 900, height: 900 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    if (init) await page.addInitScript(init);
    await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(800);
    check(`${label}: no page errors`, errors.length === 0, errors.join("; "));
    check(`${label}: the room list still renders`,
          (await page.locator("#rooms tbody tr").count()) >= 3);
    check(`${label}: the log still renders`, (await page.locator("#log .msg").count()) >= 1);
    await context.close();
  }
}

// ------------------------------------------------------------------- the live conversation
// What a person actually came for: a room that is already talking, visible without
// scrolling, updating without waiting. All three used to be false — the rooms directory came
// first and could be 200 rows tall, and the log was refreshed on a five-second timer.
{
  const context = await browser.newContext({ viewport: { width: 900, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#log .msg", { timeout: 8000 });

  // The whole conversation above the fold, and the directory below it.
  const box = await page.evaluate(() => ({
    logBottom: Math.round(document.getElementById("log").getBoundingClientRect().bottom),
    composer: Math.round(document.getElementById("composer").getBoundingClientRect().bottom),
    rooms: Math.round([...document.querySelectorAll("h2")]
      .find((h) => h.textContent === "All rooms").getBoundingClientRect().top),
    fold: innerHeight,
  }));
  check("live: the log is above the fold", box.logBottom < box.fold, JSON.stringify(box));
  check("live: so is the composer", box.composer < box.fold, JSON.stringify(box));
  check("live: the directory is below it", box.rooms > box.logBottom, JSON.stringify(box));

  // Long poll. Posted from outside the browser, so this measures the page's own latency and
  // not its own optimism about a message it just sent.
  //
  // The body is unique per run. A fixed one matches a message left in the store by the last
  // run — this file is meant to be re-run against the same CHAT_ROOT by hand — and the wait
  // then returns in 19ms against a row that was already on screen, which reads as a
  // spectacular latency result and tests nothing at all.
  const token = `live-probe-${Date.now().toString(36)}`;
  const started = Date.now();
  await fetch(`${BASE}/r/lobby/say/outsider/${token}`);
  const arrived = page.locator(`#log .msg:has(.body:text-is("${token}"))`);
  await arrived.waitFor({ timeout: 15000 });
  const latency = Date.now() - started;
  // The old timer was 5s and could be 5s late; anything under 3s can only be the long poll.
  check("live: a message arrives without waiting out a timer", latency < 3000, `${latency}ms`);

  const age = (await arrived.locator(".when").textContent()).trim();
  check("live: rows carry an age", /^\d+[smhd]$/.test(age), age);
  check("live: an arriving row is marked for the entrance animation",
        (await arrived.getAttribute("class")).includes("fresh"),
        await arrived.getAttribute("class"));
  check("live: the live dot is showing", await page.locator("#live").isVisible());

  // Scroll anchoring. A reader up in the history must not be dragged to the bottom by
  // somebody else's message — they get a count and a way down instead.
  //
  // The log is shrunk here rather than filled with twenty messages, for two reasons: twenty
  // writes is most of a 30/min budget this probe runs under on purpose, and the behaviour
  // under test reads scrollHeight, scrollTop and clientHeight and nothing else. A short log
  // with four messages in it is the same scrollable condition as a tall one with forty.
  await page.evaluate(() => {
    const log = document.getElementById("log");
    // Both, and min-height first: the log carries `min-height: 12rem` so the page lands once
    // instead of growing under the reader, and min-height beats max-height — setting only
    // the max leaves clientHeight at 192px and the log unscrollable on a fresh store.
    log.style.minHeight = "0";
    log.style.maxHeight = "60px";
    log.scrollTop = 0;
  });
  const before = await page.evaluate(() => document.getElementById("log").scrollTop);
  check("live: the log is scrollable for the anchoring checks",
        await page.evaluate(() => {
          const l = document.getElementById("log");
          return l.scrollHeight - l.clientHeight > 40;
        }));
  await fetch(`${BASE}/r/lobby/say/outsider/${token}-two`);
  await page.waitForSelector("#jump:not([hidden])", { timeout: 15000 });
  check("live: reading history is not interrupted by an arrival",
        (await page.evaluate(() => document.getElementById("log").scrollTop)) === before);
  check("live: and the arrival is offered rather than forced",
        (await page.textContent("#jump")).includes("new message"),
        await page.textContent("#jump"));
  await page.click("#jump");
  check("live: the pill takes the reader down and clears itself",
        await page.locator("#jump").isHidden());

  check("live: no page errors throughout", errors.length === 0, errors.join("; "));
  await context.close();
}

// ---------------------------------------------------------------------------- signing
// The did:key lane, which is the one part of this page a Python test can only half-check.
// tests/unit/test_humans_identity.py pins the constants the page restates; it cannot tell
// you whether WebCrypto accepts the PKCS#8 wrapper, whether the browser's Unicode tables
// agree with Python's, or whether a signature this page produces verifies on the server.
// Those are the three ways the signed lane breaks, and all three are invisible until
// somebody signed in presses Send.
//
// Runs against 127.0.0.1, which is a secure context — crypto.subtle does not exist over
// plain HTTP to any other host, so a probe against a LAN address would fail here for a
// reason that is not a bug.
{
  const context = await browser.newContext({ viewport: { width: 900, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${BASE}/humans`, { waitUntil: "domcontentloaded" });

  await page.waitForSelector("#identity:not([hidden])", { timeout: 5000 });
  check("signing: the identity row appears once Ed25519 imports", true);
  check("signing: it starts pseudonymous", (await page.textContent("#me")) === "Not signed in");

  // The one seed this repo's own signer has an answer for. `uv run scripts/sign.py did
  // --seed <this>` prints the DID asserted below, so a mismatch here means the browser and
  // the command line have stopped agreeing on what a seed means — which is exactly the
  // promise that makes pasting a seed into the composer worth offering.
  const SEED = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f";
  const EXPECTED = "did:key:z6MkehRgf7yJbgaGfYsdoAsKdBPE3dj2CYhowQdcjqSJgvVd";
  // The seed entry lives behind "Other ways in" now: one primary way to sign in, the other
  // three folded away. `<details>` needs no script, so this is a click and nothing else.
  await page.click("#keymore summary");
  await page.fill("#seed", SEED);
  await page.click("#keyuse");
  await page.waitForFunction(() => document.getElementById("me").textContent !== "Not signed in");
  const did = await page.getAttribute("#me", "title");
  check("signing: the seed yields the DID scripts/sign.py derives", did === EXPECTED, did);

  const room = `sigprobe${Date.now().toString(36)}`;
  const readRoom = async () =>
    (await fetch(`${BASE}/r/${room}?format=json`)).json();

  await page.fill("#room", room);
  await page.click("#join");
  await page.fill("#text", "hello from a browser-held key");
  await page.click("#send");
  await page.waitForTimeout(1200);
  let view = await readRoom();
  check("signing: the server accepted the signature", view.messages.length === 1,
        await page.textContent("#status"));
  check("signing: stored under the DID, not a nickname",
        view.messages[0]?.from === EXPECTED, view.messages[0]?.from);
  check("signing: the nonce is in the record, so the write re-verifies later",
        Number.isInteger(view.messages[0]?.nonce));

  // Two writes from one key into one room is where store._last_nonce bites: the second is
  // refused unless the page stepped the nonce past the first.
  await page.fill("#text", "second message");
  await page.click("#send");
  await page.waitForTimeout(1200);
  view = await readRoom();
  check("signing: a second write is accepted", view.messages.length === 2,
        await page.textContent("#status"));
  if (view.messages.length === 2)
    check("signing: the nonce strictly increased",
          view.messages[1].nonce > view.messages[0].nonce,
          `${view.messages[0].nonce} -> ${view.messages[1].nonce}`);

  // The sweep. One character from three of the six categories store.clean_text replaces:
  // U+200B (Cf), U+0007 (Cc), U+2028 (Zl). If the page's regex and Python's
  // unicodedata.category disagree on any of them, the signature covers different bytes
  // than the server stores and this comes back 403.
  const messy = `zero${String.fromCharCode(0x200b)}width${String.fromCharCode(0x07)}`
              + `and${String.fromCharCode(0x2028)}sep`;
  await page.evaluate((t) => { document.getElementById("text").value = t; }, messy);
  await page.click("#send");
  await page.waitForTimeout(1200);
  view = await readRoom();
  check("signing: invisibles swept the same way the server sweeps them",
        view.messages[2]?.text === "zero width and sep",
        JSON.stringify(view.messages[2]?.text));

  // domcontentloaded, not networkidle — and this is now the only option rather than the
  // better one. The page reads its room by long poll, so it deliberately holds a request
  // open for up to ten seconds at all times and the network never goes quiet. Every
  // navigation in this file waits for the thing it is about to assert instead.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#identity:not([hidden])", { timeout: 8000 });
  await page.waitForFunction(() => document.getElementById("me").textContent !== "Not signed in",
                             null, { timeout: 8000 });
  check("signing: the identity survives a reload",
        (await page.getAttribute("#me", "title")) === EXPECTED);

  // Signing out has to land back on the lane the whole page is built around, not on a
  // half-state that can no longer post at all.
  await page.click("#keyout");
  check("signing: sign-out clears the badge",
        (await page.textContent("#me")) === "Not signed in");
  await page.fill("#room", room);
  await page.click("#join");
  await page.fill("#nick", "plain");
  await page.fill("#text", "unsigned again");
  await page.click("#send");
  await page.waitForTimeout(1200);
  view = await readRoom();
  check("signing: the pseudonymous lane still works after sign-out",
        view.messages[3]?.from === "plain", view.messages[3]?.from);

  check("signing: no page errors throughout", errors.length === 0, errors.join("; "));
  await context.close();
}

// -------------------------------------------------------------------- passkey + delegation
// Two things a Python test cannot reach at all: whether an authenticator's PRF output can
// actually stand in as an Ed25519 seed, and whether the identity comes back on a browser
// with nothing in it — which is the entire reason the passkey path exists.
//
// Driven with Chrome's virtual authenticator over CDP, `hasPrf: true`. Note the RP ID
// constraint: WebAuthn refuses a bare IP address as a relying party, so this section (alone
// among all of them) talks to `localhost` rather than 127.0.0.1. Same origin to the server,
// a valid registrable domain to WebAuthn.
{
  const HOST = BASE.replace("127.0.0.1", "localhost");
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable", { enableUI: false });
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2", ctap2Version: "ctap2_1", transport: "internal",
      hasResidentKey: true, hasUserVerification: true, hasPrf: true,
      automaticPresenceSimulation: true, isUserVerified: true,
    },
  });

  const ready = () => page.waitForSelector("#identity:not([hidden])", { timeout: 8000 });
  const signedIn = () =>
    page.waitForFunction(
      () => document.getElementById("me").textContent !== "Not signed in",
      null, { timeout: 15000 });

  await page.goto(`${HOST}/humans`, { waitUntil: "domcontentloaded" });
  await ready();
  check("passkey: the primary way in is on the surface",
        await page.locator("#keypass").isVisible());
  check("passkey: enrolling another is folded away until asked for",
        !(await page.locator("#keypassnew").isVisible()));
  await page.click("#keymore summary");
  check("passkey: and the disclosure reveals it",
        await page.locator("#keypassnew").isVisible());

  // Discovery with nothing enrolled must explain itself and must not quietly enrol. This is
  // the branch that used to be reached by inference from stored state, and got it backwards.
  await page.click("#keypass");
  await page.waitForTimeout(2500);
  check("passkey: discovery with none enrolled points at the other button",
        (await page.textContent("#status")).includes("no passkey used"),
        await page.textContent("#status"));
  check("passkey: and signed nobody in",
        (await page.textContent("#me")) === "Not signed in");

  await page.click("#keypassnew");
  await signedIn();
  const did = await page.getAttribute("#me", "title");
  check("passkey: enrolment derives a did:key",
        /^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$/.test(did), did);
  check("passkey: the seed is never written to storage",
        (await page.evaluate(() => localStorage.getItem("technocore.seed"))) === null);

  // The whole point, in one check: wipe every byte of local state and get the same identity
  // back from the authenticator alone.
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "domcontentloaded" });
  await ready();
  check("passkey: a wiped browser starts signed out",
        (await page.textContent("#me")) === "Not signed in");
  await page.click("#keypass");
  await signedIn();
  check("passkey: the same passkey recovers the same DID from empty storage",
        (await page.getAttribute("#me", "title")) === did,
        await page.getAttribute("#me", "title"));

  // ---- delegation ----
  check("delegation: the agents panel appears once signed in",
        !(await page.locator("#agents").isHidden()));
  check("delegation: and stays folded until asked for",
        !(await page.locator("#agent-did").isVisible()));
  await page.click("#agents summary");

  const fp = await page.evaluate(async (d) => {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(d));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0"))
      .join("").slice(0, 16);
  }, did);
  const notePath = `/kv/did-${fp.slice(0, 2)}/${fp.slice(2)}`;

  // Seed the note with the thing a real DID note already holds. A note is ONE line — the
  // server's sweep turns every newline into a space — so this is what makes the delegations
  // below land in a note that is not empty, which is the case that a line-oriented parser
  // silently loses (PR #719 review).
  await fetch(`${HOST}${notePath}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: "mailbox: mb-probe" }),
  });

  const AGENT = "did:key:z6MkqGC3nWZhYieEVTVDKW5v588CiGfsDSmRVG9ZwwWTvLSK";
  const AGENT2 = "did:key:z6MkehRgf7yJbgaGfYsdoAsKdBPE3dj2CYhowQdcjqSJgvVd";
  const delegate = async (agent, scope, days) => {
    await page.fill("#agent-did", agent);
    await page.fill("#agent-scope", scope);
    await page.fill("#agent-days", days);
    await page.click("#delegate");
    await page.waitForTimeout(2000);
  };

  await delegate(AGENT, "r:lobby", "30");
  check("delegation: it verifies against the issuing key and lists as live",
        (await page.locator(".deleg.ok .state").count()) === 1,
        await page.textContent("#status"));
  check("delegation: the row names the scope",
        (await page.textContent(".deleg")).includes("r:lobby"),
        await page.textContent(".deleg"));

  // The regression. Two records plus a mailbox, all in one line, all still findable.
  await delegate(AGENT2, "kv:plans", "10");
  check("delegation: a second one is found beside the first in a one-line note",
        (await page.locator(".deleg.ok .state").count()) === 2,
        await page.textContent("#status"));

  const note = await (await fetch(`${HOST}${notePath}`)).text();
  check("delegation: published to the DID note path",
        note.includes(`delegate: ${AGENT}`) && note.includes(`delegate: ${AGENT2}`), notePath);
  check("delegation: the note's existing content survived the append",
        note.includes("mailbox: mb-probe"), note.slice(0, 120));
  check("delegation: and the note really is a single line",
        note.trimEnd().split("\n").filter((l) => l.includes("delegate:")).length === 1);

  // Every refusal is client-side; none of these may reach the network. Asserted on the
  // delegation count rather than on the status badge: the room poll writes "seq N" over that
  // badge about once a second, so reading it after a fixed wait is a race the probe loses
  // roughly whenever the two line up.
  for (const [label, agent, scope, days] of [
    ["a non-DID agent", "not-a-did", "*", "30"],
    ["a scope outside the grammar", AGENT, "room:lobby", "30"],
    ["a zero-day expiry", AGENT, "*", "0"],
    ["delegating to itself", did, "*", "30"],
  ]) {
    await page.fill("#agent-did", agent);
    await page.fill("#agent-scope", scope);
    await page.fill("#agent-days", days);
    await page.click("#delegate");
    await page.waitForTimeout(800);
    check(`delegation: refuses ${label}`,
          (await page.locator(".deleg").count()) === 2,
          `${await page.locator(".deleg").count()} rows`);
  }
  const after = await (await fetch(`${HOST}${notePath}`)).text();
  check("delegation: no refusal reached the note",
        (after.match(/delegate:/g) || []).length === 2);

  // Enrolling a SECOND passkey while one already exists has to sign the reader in as the
  // credential just created. Deriving from an unconstrained get() here would hand back
  // whichever credential the authenticator picked — a new key made and silently discarded,
  // under a DID the reader did not just mint (PR #719 review). Runs last: it leaves two
  // discoverable credentials behind, which makes any later unconstrained discovery ambiguous.
  await page.click("#keyout");
  await page.click("#keymore summary");
  await page.click("#keypassnew");
  await signedIn();
  check("passkey: enrolling a second one derives from the new credential, not an old one",
        (await page.getAttribute("#me", "title")) !== did,
        await page.getAttribute("#me", "title"));

  check("passkey + delegation: no page errors throughout",
        errors.length === 0, errors.join("; "));
  await context.close();
}


await browser.close();
console.log(failures ? `\n${failures} check(s) FAILED` : "\nall checks passed");
process.exit(failures ? 1 : 0);
