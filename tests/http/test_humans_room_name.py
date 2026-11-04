"""The Room box must not open a room the server will refuse to read.

open() takes whatever was typed. A name outside NAME_RE is a 400 on every read, and the
pump answers a failed read by retrying in 3 s, so one typo became a bare "HTTP 400" badge
and a read every 3 s until the reader left. Node executes the served open(); the 400
comes from the real local app.
"""

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client
NODE = shutil.which("node")

OPEN = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const html = input.html;
for (const m of ['  var NAME_RE =', '  function parseHash()', '/* open:start */', '/* open:end */']) {
  if (html.indexOf(m) < 0) throw new Error('marker missing from humans.html: ' + m);
}
const re = html.slice(html.indexOf('  var NAME_RE ='), html.indexOf('  function parseHash()'));
// open() is fenced by explicit markers, so a comment or a function added next to it cannot
// silently truncate what is executed here.
const source = html.slice(html.indexOf('/* open:start */'), html.indexOf('/* open:end */'));
if (!source.includes('function open(')) throw new Error('open() is not between its markers');
let calls, failed;
const context = vm.createContext({
  roomEl: {value: ''}, log: {textContent: '', appendChild: () => {}},
  document: {createElement: () => ({}), hidden: false},
  setHash: () => calls.push('setHash'), renderJump: () => calls.push('renderJump'),
  stopPoll: () => calls.push('stopPoll'), pump: () => calls.push('pump:' + context.room),
  fail: m => failed.push(String(m)), loadRooms: () => {},
  setInterval: () => { calls.push('setInterval'); return 99; },
  clearInterval: () => calls.push('clearInterval'),
});
vm.runInContext(re + source + '; var unseen, firstPaint, targetRevealed, primed;', context);
const out = [];
for (const typed of input.typed) {
  // A reader already in a working room: not the defaults, with a live rooms timer.
  calls = []; failed = [];
  Object.assign(context, {room: 'standup', since: 42, targetSeq: 7, roomsTimer: 5});
  context.roomEl.value = 'standup'; context.log.textContent = 'kept history';
  context.open(typed);
  out.push({typed, room: context.room, since: context.since, targetSeq: context.targetSeq,
    roomsTimer: context.roomsTimer, roomEl: context.roomEl.value,
    log: context.log.textContent, calls: calls.slice(), failed: failed.slice()});
}
console.log(JSON.stringify(out));
"""


def _open(client, typed):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", OPEN],
        input=json.dumps({"html": client.get("/humans").text, "typed": typed}),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return {r["typed"]: r for r in json.loads(result.stdout)}


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the served JavaScript")
def test_room_box_refuses_a_name_the_server_cannot_read(client):
    bad = ["My Room", "café", "-lead", "a.b"]
    for name in bad:
        assert client.get(f"/r/{name.lower()}?format=json").status_code == 400
    # Whitespace-only is truthy, so it is not the lobby: it trims to "", which NAME_RE
    # refuses, and must take the same no-side-effects path as the names above.
    blank = [" "]
    observed = _open(client, [*bad, *blank, " Standup "])
    for name in [*bad, *blank]:
        r = observed[name]
        # Refused with the rule, not a bare HTTP status.
        assert len(r["failed"]) == 1, r
        msg = r["failed"][0]
        assert "lowercase letters, digits, - and _" in msg
        assert "^[a-z0-9][a-z0-9_-]{0,47}$" in msg and "HTTP" not in msg
        # The room being read is untouched: state, timers, history, and no side effects.
        assert (r["room"], r["since"], r["targetSeq"], r["roomsTimer"]) == ("standup", 42, 7, 5)
        assert (r["roomEl"], r["log"]) == ("standup", "kept history")
        assert r["calls"] == [], r
    # A valid name still opens, trimmed and lowercased as before, with one pump.
    ok = observed[" Standup "]
    assert ok["failed"] == []
    assert ok["room"] == "standup"
    assert [c for c in ok["calls"] if c.startswith("pump")] == ["pump:standup"]
    assert "stopPoll" in ok["calls"] and "setHash" in ok["calls"]


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the served JavaScript")
def test_an_empty_room_box_still_opens_the_lobby(client):
    # open() reads `next || 'lobby'`, so an empty box is the lobby, not a refused name.
    r = _open(client, [""])[""]
    assert r["failed"] == [], r
    assert r["room"] == "lobby" and r["roomEl"] == "lobby"
    assert [c for c in r["calls"] if c.startswith("pump")] == ["pump:lobby"]
