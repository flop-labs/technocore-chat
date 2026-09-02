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
 * Checked 2026-09-02, 66 checks, all passing — expected shape:
 *   desktop 900px   5 columns, copy icon is an <svg> with an accessible name
 *   copy            writes the #r/<room> permalink, swaps glyph + label, restores after 1.2s
 *   filter          narrows rows, counts against LOADED rooms, survives the 5s refresh
 *   open a room     scrolls the Room heading into view
 *   Enter in filter opens the top match
 *   delayed send    a room A response cannot erase a newer room B draft
 *   mobile 390px    4 columns (byte column dropped), no horizontal scroll at 320-1280px
 *   webmcp          eight tools register on load, measured through the browser's own
 *                   getTools()/executeTool() (Chrome 151 + --enable-features=WebMCP, set
 *                   in the launch args; a stub stands in where the flag does nothing),
 *                   hints are right, and every tool actually reaches the server
 *   webmcp absent   the page is unchanged with no modelContext, and with one that throws
 *
 * The webmcp section posts a message, which reorders /rooms — it runs last, after every
 * check that reads the seeded list.
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

// -------------------------------------------------------------- delayed successful sends
// This section posts messages and therefore runs after the layout checks that depend on the
// initial room ordering and row heights.
async function delayedSendCase({ label, draftEdits = [], nextRoom, expected }) {
  const context = await browser.newContext({ viewport: { width: 900, height: 900 } });
  const page = await context.newPage();
  let releaseResponse;
  let markPosted;
  const mayRespond = new Promise((resolve) => { releaseResponse = resolve; });
  const posted = new Promise((resolve) => { markPosted = resolve; });

  await page.route(`${BASE}/r/lobby`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();       // the write landed; hold only its response
    markPosted();
    await mayRespond;
    await route.fulfill({ response });
  });
  await page.goto(`${BASE}/humans#r/lobby`, { waitUntil: "networkidle" });

  await page.fill("#text", "sent to room A");
  await page.locator("#send").click();
  await posted;
  if (nextRoom) {
    await page.fill("#room", nextRoom);
    await page.locator("#join").click();
  }
  for (const draft of draftEdits) await page.fill("#text", draft);

  const postResponse = page.waitForResponse((response) =>
    response.request().method() === "POST"
      && new URL(response.url()).pathname === "/r/lobby",
  );
  const currentRoom = nextRoom || "lobby";
  const responsePoll = page.waitForRequest((request) =>
    request.method() === "GET"
      && new URL(request.url()).pathname === `/r/${currentRoom}`
      && new URL(request.url()).searchParams.get("format") === "json",
  );
  releaseResponse();
  const response = await postResponse;
  await responsePoll; // send() clears or preserves the draft before starting this poll
  check(`${label}: POST succeeded`, response.ok(), `HTTP ${response.status()}`);
  const actual = await page.inputValue("#text");
  check(label, actual === expected, JSON.stringify(actual));
  await context.close();
}

console.log("delayed successful sends");
await delayedSendCase({
  label: "an unchanged draft clears",
  expected: "",
});
await delayedSendCase({
  label: "a newer draft in the same room survives",
  draftEdits: ["newer lobby draft"],
  expected: "newer lobby draft",
});
await delayedSendCase({
  label: "an edited draft restored to the same text survives",
  draftEdits: ["temporary edit", "sent to room A"],
  expected: "sent to room A",
});
await delayedSendCase({
  label: "a room switch preserves the unchanged composer",
  nextRoom: "standup",
  expected: "sent to room A",
});
await delayedSendCase({
  label: "room A's response preserves room B's draft",
  nextRoom: "standup",
  draftEdits: ["unsent room B draft"],
  expected: "unsent room B draft",
});

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
  await page.goto(`${BASE}/humans`, { waitUntil: "networkidle" });
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
    await page.goto(`${BASE}/humans`, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    check(`${label}: no page errors`, errors.length === 0, errors.join("; "));
    check(`${label}: the room list still renders`,
          (await page.locator("#rooms tbody tr").count()) >= 3);
    check(`${label}: the log still renders`, (await page.locator("#log .msg").count()) >= 1);
    await context.close();
  }
}


await browser.close();
console.log(failures ? `\n${failures} check(s) FAILED` : "\nall checks passed");
process.exit(failures ? 1 : 0);
