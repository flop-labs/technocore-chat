"""A resolved permalink must stop directing later live-message scrolling.

Node executes the served render/pump/open functions and the real navigation/scroll
listeners. A small DOM spy records scroll requests; Chromium covers actual layout.
"""

import json
import shutil
import subprocess

import _client
import pytest

import store

client = _client.client
NODE = shutil.which("node")

SCROLL = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const html = input.html;
const section = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));
const centered = [], pending = [], requests = [], errors = [];
const timers = new Map(), windowEvents = new Map();
let timerId = 0;
class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.className = '';
    this.hidden = false; this.events = new Map(); this.top = 0;
  }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  get firstChild() { return this.children[0]; }
  get clientHeight() { return 180; }
  get scrollHeight() { return this.children.length * 60; }
  get scrollTop() { return this.top; }
  set scrollTop(value) {
    this.top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight));
  }
  appendChild(child) { this.children.push(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener(name, fn) { this.events.set(name, fn); }
  fire(name) { this.events.get(name)(); }
  querySelectorAll(selector) {
    const matches = [];
    for (const child of this.children) {
      if (selector.slice(1).split('.').every(s => child.className.split(' ').includes(s))) {
        matches.push(child);
      }
      matches.push(...child.querySelectorAll(selector));
    }
    return matches;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() {
    centered.push(this.firstChild.textContent);
    log.scrollTop = log.children.indexOf(this) * 60 - 60;
  }
}
const log = new Element(), jump = new Element();
const context = vm.createContext({
  log, jumpEl: jump, liveEl: new Element(), roomEl: new Element(), MAX_ROWS: 200,
  room: 'lobby', since: 0, targetSeq: 0, roomsTimer: null,
  NAME_RE: /^[a-z0-9][a-z0-9_-]{0,47}$/,
  location: {hash: '', origin: 'https://test.invalid', pathname: '/humans'},
  history: {replaceState: (_state, _title, hash) => { context.location.hash = hash; }},
  document: {hidden: false, createElement: () => new Element()},
  window: {addEventListener: (name, fn) => windowEvents.set(name, fn)},
  AbortController, Date, encodeURIComponent,
  setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id; },
  clearTimeout: id => timers.delete(id), setInterval: () => ++timerId, clearInterval: () => {},
  copyButton: label => { const e = new Element(); e.textContent = label; return e; },
  ago: () => '', beat: () => {}, fail: message => errors.push(message), loadRooms: () => {},
  fetch: (path, opts) => new Promise((resolve, reject) => {
    requests.push(path);
    pending.push(view => resolve({ok: true, json: async () => view}));
    opts.signal.addEventListener('abort', () => reject(new DOMException('', 'AbortError')));
  }),
});
vm.runInContext(
  section('  function parseHash()', '  function bindCopy(')
  + section('  function atBottom()', '  // -------------------------------------------------------------------------- identity')
  + section("  window.addEventListener('hashchange'", '  // A hidden tab holds no waiter slot'),
  context,
);
const settled = () => new Promise(setImmediate);
async function reply(view) {
  if (pending.length !== 1) throw new Error(`expected one poll, got ${pending.length}`);
  pending.shift()(view);
  await settled();
}
async function poll(view) {
  const tick = timers.get(context.pollTimer);
  if (!tick) throw new Error('the next live poll was not scheduled');
  timers.delete(context.pollTimer);
  tick();
  await reply(view);
}
function snapshot() {
  return {centers: centered.length, lastCentered: centered.at(-1), top: log.scrollTop,
    bottom: log.scrollHeight - log.clientHeight, unseen: context.unseen,
    jumpHidden: jump.hidden, target: context.targetSeq,
    highlighted: log.querySelector('.msg.target')?.firstChild.textContent || null,
    since: context.since, hash: context.location.hash};
}
(async () => {
  const out = {};
  context.open('scroll', 10);
  await reply(input.views.initial);
  out.initial = snapshot();
  log.scrollTop = 120;
  log.fire('scroll');
  await poll(input.views.history);
  out.history = snapshot();
  jump.fire('click');
  out.jumped = snapshot();
  await poll(input.views.latest);
  out.latest = snapshot();
  log.scrollTop = log.scrollHeight;
  log.fire('scroll');
  await poll(input.views.manual);
  out.manual = snapshot();
  context.location.hash = '#r/scroll/40';
  windowEvents.get('hashchange')();
  await reply(input.views.reopened);
  out.reopened = snapshot();
  log.scrollTop = 120;
  log.fire('scroll');
  await poll(input.views.reopenedHistory);
  out.reopenedHistory = snapshot();
  context.location.hash = '#r/scroll';
  windowEvents.get('hashchange')();
  await reply(input.views.ordinary);
  out.ordinary = snapshot();
  log.scrollTop = 120;
  log.fire('scroll');
  await poll(input.views.ordinaryHistory);
  out.ordinaryHistory = snapshot();
  log.scrollTop = log.scrollHeight;
  log.fire('scroll');
  out.manualCleared = snapshot();
  await poll(input.views.ordinaryLatest);
  out.ordinaryLatest = snapshot();
  out.requests = requests;
  out.errors = errors;
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.fixture()
def scroll_behavior(client, tmp_path):
    if NODE is None:
        pytest.skip("Node is needed to execute the served JavaScript")
    for seq in range(1, 60):
        store.append(tmp_path, "scroll", "bot", f"message {seq}")
    views = {"initial": client.get("/r/scroll?since=9&format=json").json()}
    for name, seq in [("history", 60), ("latest", 61), ("manual", 62)]:
        store.append(tmp_path, "scroll", "bot", f"message {seq}")
        views[name] = client.get(f"/r/scroll?since={seq - 1}&format=json").json()
    views["reopened"] = client.get("/r/scroll?since=39&format=json").json()
    store.append(tmp_path, "scroll", "bot", "message 63")
    views["reopenedHistory"] = client.get("/r/scroll?since=62&format=json").json()
    views["ordinary"] = client.get("/r/scroll?since=0&format=json").json()
    for name, seq in [("ordinaryHistory", 64), ("ordinaryLatest", 65)]:
        store.append(tmp_path, "scroll", "bot", f"message {seq}")
        views[name] = client.get(f"/r/scroll?since={seq - 1}&format=json").json()
    assert NODE is not None
    run = subprocess.run(
        [NODE, "-e", SCROLL],
        input=json.dumps({"html": client.get("/humans").text, "views": views}),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(run.stdout)


def test_permalink_centers_and_highlights_its_initial_message(scroll_behavior):
    initial = scroll_behavior["initial"]
    assert initial["centers"] == 1
    assert initial["lastCentered"] == initial["highlighted"] == "#10"
    assert initial["target"] == 10
    assert initial["hash"] == "#r/scroll/10"


def test_resolved_permalink_preserves_manual_history_reading(scroll_behavior):
    initial, history = (scroll_behavior[key] for key in ("initial", "history"))
    assert history["centers"] == initial["centers"]
    assert history["top"] == 120
    assert history["unseen"] == 1 and not history["jumpHidden"]
    assert history["highlighted"] == "#10"


def test_resolved_permalink_respects_jump_to_latest(scroll_behavior):
    jumped, latest = (scroll_behavior[key] for key in ("jumped", "latest"))
    assert jumped["top"] == jumped["bottom"]
    assert jumped["unseen"] == 0 and jumped["jumpHidden"]
    assert latest["centers"] == jumped["centers"]
    assert latest["top"] == latest["bottom"]
    assert latest["highlighted"] == "#10"


def test_resolved_permalink_respects_manually_returning_to_latest(scroll_behavior):
    latest, manual = (scroll_behavior[key] for key in ("latest", "manual"))
    assert manual["centers"] == latest["centers"]
    assert manual["top"] == manual["bottom"]
    assert manual["unseen"] == 0 and manual["jumpHidden"]


def test_new_same_room_permalink_centers_and_keeps_navigation_state(scroll_behavior):
    manual, reopened = (scroll_behavior[key] for key in ("manual", "reopened"))
    assert reopened["centers"] == manual["centers"] + 1
    assert reopened["lastCentered"] == reopened["highlighted"] == "#40"
    assert reopened["target"] == 40
    assert reopened["hash"] == "#r/scroll/40"


def test_new_same_room_permalink_also_releases_scroll_control(scroll_behavior):
    reopened, history = (scroll_behavior[key] for key in ("reopened", "reopenedHistory"))
    assert history["centers"] == reopened["centers"]
    assert history["top"] == 120
    assert history["unseen"] == 1 and not history["jumpHidden"]
    assert history["highlighted"] == "#40"


def test_ordinary_room_keeps_its_scroll_and_jump_behavior(scroll_behavior):
    ordinary, history, cleared, latest = (
        scroll_behavior[key]
        for key in ("ordinary", "ordinaryHistory", "manualCleared", "ordinaryLatest")
    )
    assert ordinary["target"] == 0 and ordinary["highlighted"] is None
    assert ordinary["top"] == ordinary["bottom"]
    assert history["top"] == 120
    assert history["unseen"] == 1 and not history["jumpHidden"]
    assert cleared["unseen"] == 0 and cleared["jumpHidden"]
    assert latest["top"] == latest["bottom"]
    assert latest["centers"] == ordinary["centers"]


def test_permalink_scrolling_does_not_interrupt_live_polling(scroll_behavior):
    assert scroll_behavior["errors"] == []
    assert [scroll_behavior[key]["since"] for key in ("initial", "history", "latest")] == [
        59,
        60,
        61,
    ]
    assert [path.split("since=")[1] for path in scroll_behavior["requests"]] == [
        "9",
        "59&wait=10",
        "60&wait=10",
        "61&wait=10",
        "39",
        "62&wait=10",
        "0",
        "63&wait=10",
        "64&wait=10",
    ]
