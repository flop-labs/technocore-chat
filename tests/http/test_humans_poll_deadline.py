"""A stalled room response must expire without treating navigation as a failure."""

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client
NODE = shutil.which("node")
POLL = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const {html, mode} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const timers = new Map(), requests = [], errors = [];
let now = 0, id = 0;
const flush = () => new Promise(setImmediate);
const view = {first_seq: null, last_seq: 7, messages: []};
const env = vm.createContext({
  room: 'watch', since: 7, targetSeq: 0, document: {hidden: false},
  liveEl: {hidden: false}, Date: {now: () => now}, AbortController,
  restamp() {}, beat() {}, fail: text => errors.push(text),
  setTimeout(fn, ms) { timers.set(++id, {fn, at: now + ms}); return id; },
  clearTimeout: key => timers.delete(key),
  fetch: (path, {signal}) => new Promise((resolve, reject) => {
    const request = {path, signal, reply: () => resolve({ok: true, json: async () => view})};
    requests.push(request);
    signal.addEventListener('abort', () => reject(new DOMException('', 'AbortError')));
    if (mode === 'body') resolve({ok: true, json: () => new Promise((done, fail) => {
      signal.addEventListener('abort', () => fail(new DOMException('', 'AbortError')));
    })});
    if (mode === 'refused') resolve({ok: false, status: 503});
  }),
});
vm.runInContext(html.slice(html.indexOf('  var POLL_WAIT ='),
                          html.indexOf('  function open(')), env);
env.primed = true;
async function advance(ms) {
  now += ms;
  for (const [key, timer] of [...timers]) {
    if (timer.at <= now) { timers.delete(key); timer.fn(); await flush(); }
  }
}
(async () => {
  env.pump(); await flush();
  assert.match(requests[0].path, /since=7&wait=10$/);
  if (mode === 'cancel' || mode === 'replace') {
    env.stopPoll();
    if (mode === 'replace') { env.pump(); requests[1].reply(); }
    await flush();
    assert.equal(errors.length, 0, 'intentional abort is silent');
    if (mode === 'cancel') assert.equal(timers.size, 0, 'cancel removes its deadline');
  } else if (mode === 'success') {
    await advance(10000);
    assert.equal(requests[0].signal.aborted, false, 'allow the full server wait');
    requests[0].reply(); await flush();
    assert.equal(errors.length, 0);
    assert.equal(timers.size, 1, 'only the next poll remains after parsing');
  } else if (mode === 'refused') {
    assert.deepEqual(errors, ['HTTP 503']);
    assert.equal(timers.size, 1, 'HTTP failures clear their deadline');
  } else {
    await advance(10000);
    assert.equal(requests[0].signal.aborted, false, 'allow the full server wait');
    await advance(15000);
    assert.equal(requests[0].signal.aborted, true, 'stalled response must be aborted');
    assert.equal(env.liveEl.hidden, true, 'a timed-out log is not live');
    assert.match(errors[0], /timed out/);
    assert.equal(env.since, 7, 'retain the unread cursor');
    assert.equal(timers.size, 1, 'one retry is scheduled');
    await advance(3000);
    assert.equal(requests.length, 2, 'resume reading after the timeout');
    assert.match(requests[1].path, /since=7&wait=10$/);
  }
  console.log('ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the served JavaScript")
@pytest.mark.parametrize("mode", ["headers", "body", "success", "refused", "cancel", "replace"])
def test_room_poll_deadline(client, mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", POLL],
        input=json.dumps({"html": client.get("/humans").text, "mode": mode}),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
