"""Execute the page's send path with a real signature held across identity/room changes.

Node supplies WebCrypto; the browser probe separately exercises the visible controls and
server acceptance. The gate makes the race deterministic without a timing-sensitive sleep.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")

PROBE = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const mode = process.argv[1];
const moveRoom = process.argv[2] === 'move';
const html = readFileSync('src/humans.html', 'utf8');
const send = html.match(/  function send\(\) \{[\s\S]*?\n  \}/)?.[0];
const logout = html.match(/keyOutEl\.addEventListener\('click', function \(\) \{([\s\S]*?)\n  \}\);/)?.[1];
// Sign-out now starts the next identity attempt, so the extracted handler needs the
// page's real counter and ceremony guard in scope as well.
const attemptVar = html.match(/  var identityAttempt = 0;\n/)?.[0];
const beginIdentity = html.match(/  function beginIdentity\(\) \{[\s\S]*?\n  \}/)?.[0];
assert.ok(send && logout && attemptVar && beginIdentity, 'send and sign-out handlers exist');
const watchdog = setTimeout(() => { throw new Error('send did not settle'); }, 5000);

async function identity() {
  const pair = await webcrypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const raw = await webcrypto.subtle.exportKey('raw', pair.publicKey);
  let n = BigInt('0xed01' + Buffer.from(raw).toString('hex')), encoded = '';
  const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  while (n) { encoded = alphabet[Number(n % 58n)] + encoded; n /= 58n; }
  return { did: 'did:key:z' + encoded, key: pair.privateKey, publicKey: pair.publicKey };
}
const original = await identity(), replacement = await identity();
let release, started, settled;
const gate = new Promise(resolve => { release = resolve; });
const signing = new Promise(resolve => { started = resolve; });
const done = new Promise(resolve => { settled = resolve; });
const posts = [], errors = [];
const env = {
  TextEncoder, Promise, String, encodeURIComponent,
  me: original, room: 'identityprobe', draftRevision: 0,
  textEl: { value: 'hello', focus() {} }, nickEl: { value: 'guest' },
  swept: value => value, nextNonce: () => 123,
  bytesB64u: value => Buffer.from(value).toString('base64url'),
  crypto: { subtle: { sign(...args) {
    started();
    return webcrypto.subtle.sign(...args).then(async signature => {
      await gate;
      return signature;
    });
  } } },
  fetch: async (url, options) => {
    posts.push({ url, options, payload: JSON.parse(options.body) });
    setImmediate(settled); // Observe completion even when the UI has moved to another room.
    return { ok: true };
  },
  fail: message => { errors.push(message); settled(); },
  stopPoll() {}, pump() { settled(); },
  SEED_KEY: 'seed', drop() {}, renderIdentity() {}, say() {},
};
vm.createContext(env);
vm.runInContext(send + '\nsend();', env);
await signing;
if (mode !== 'unchanged')
  vm.runInContext(attemptVar + 'var ceremony = null; function hold() {}\n' + beginIdentity + '\n' + logout, env);
if (mode === 'switch') env.me = replacement;
if (moveRoom) {
  env.room = 'otherroom';
  env.textEl.value = 'a draft for the other room';
}
assert.equal(posts.length, 0, 'no write before signing completes');
release();
await done;
assert.deepEqual(errors, [], 'changing identity must not crash the pending send');
assert.equal(posts.length, 1, 'exactly one write');
const { url, options, payload } = posts[0];
assert.equal(url, '/r/identityprobe');
assert.equal(options.method, 'POST');
assert.equal(payload.did, original.did, 'the clicked identity owns the pending send');
assert.equal(payload.text, 'hello');
assert.equal(payload.nonce, '123');
const signature = Buffer.from(payload.sig, 'base64url');
const canonical = new TextEncoder().encode('identityprobe|123|hello');
assert.equal(await webcrypto.subtle.verify('Ed25519', original.publicKey, signature, canonical), true);
assert.equal(await webcrypto.subtle.verify('Ed25519', replacement.publicKey, signature, canonical), false);
assert.equal(env.me, mode === 'unchanged' ? original : mode === 'switch' ? replacement : null,
             'finishing the send must not restore the previous identity');
assert.equal(env.room, moveRoom ? 'otherroom' : 'identityprobe',
             'finishing the send must not navigate back to the previous room');
assert.equal(env.textEl.value, moveRoom ? 'a draft for the other room' : '',
             'only a completed send in the displayed room clears its composer');
clearTimeout(watchdog);
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the page's send path")
@pytest.mark.parametrize("mode", ["unchanged", "logout", "switch"])
@pytest.mark.parametrize("navigation", ["stay", "move"])
def test_pending_send_keeps_the_identity_and_room_that_started_it(mode, navigation):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROBE, mode, navigation],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


DRAFT_PROBE = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const mode = process.argv[1];
const html = readFileSync('src/humans.html', 'utf8');
const send = html.match(/  function send\(\) \{[\s\S]*?\n  \}/)?.[0];
const initial = html.match(/  var room = 'lobby'[^\n]+/)?.[0];
const input = html.match(/  textEl\.addEventListener\('input', [^\n]+/)?.[0] || '';
assert.ok(send && initial, 'execute the actual composer state and send handler');
const handlers = {}, posts = [], errors = [];
let focuses = 0, polls = 0;
const env = {
  Promise, String, encodeURIComponent, me: null,
  textEl: {
    value: '  original draft  ',
    focus() { focuses++; },
    addEventListener(type, callback) { handlers[type] = callback; },
  },
  nickEl: { value: 'guest' },
  fetch(url, options) {
    return new Promise((resolve, reject) => {
      posts.push({ url, payload: JSON.parse(options.body), resolve, reject });
    });
  },
  stopPoll() {}, pump() { polls++; }, fail(message) { errors.push(message); },
};
vm.createContext(env);
vm.runInContext(initial + '\n' + input + '\n' + send, env);
const tick = () => new Promise(resolve => setImmediate(resolve));
function edit(value) {
  env.textEl.value = value;
  handlers.input?.();
}
async function click() { vm.runInContext('send();', env); await tick(); }
async function finish(index, ok = true) {
  posts[index].resolve({ ok, text: async () => 'request refused\n' });
  await tick();
}
await click();
assert.equal(posts[0].url, '/r/lobby');
assert.equal(posts[0].payload.text, 'original draft', 'wire text remains trimmed');

let expected = '', expectedFocuses = 1, expectedPolls = 1;
if (mode === 'edited' || mode === 'refused' || mode === 'network') {
  edit('newer draft'); expected = 'newer draft'; expectedFocuses = 0;
} else if (mode === 'restored') {
  edit('temporary'); edit('  original draft  ');
  expected = '  original draft  '; expectedFocuses = 0;
} else if (mode === 'programmatic') {
  env.textEl.value = 'replacement without an input event';
  expected = env.textEl.value; expectedFocuses = 0;
} else if (mode === 'other-room' || mode === 'returned-room') {
  env.room = 'elsewhere'; edit('another room draft');
  if (mode === 'returned-room') { env.room = 'lobby'; edit('newer lobby draft'); }
  expected = env.textEl.value; expectedFocuses = 0;
  expectedPolls = mode === 'other-room' ? 0 : 1;
} else if (mode === 'out-of-order') {
  edit('second message'); await click();
  assert.equal(posts[1].payload.text, 'second message');
  await finish(1);
  assert.equal(env.textEl.value, '', 'the latest successful send clears its own draft');
  edit('third, still unsent'); expected = env.textEl.value; expectedPolls = 2;
} else if (mode === 'duplicate-completion') {
  await click();
  await finish(1);
  assert.equal(env.textEl.value, '');
  edit('  original draft  '); expected = env.textEl.value; expectedPolls = 2;
}

if (mode === 'network') {
  posts[0].reject(new Error('offline')); await tick(); expectedPolls = 0;
} else {
  await finish(0, mode !== 'refused');
  if (mode === 'refused') expectedPolls = 0;
}
assert.equal(env.textEl.value, expected, 'completion must not erase a newer unsent draft');
assert.equal(focuses, expectedFocuses, 'a stale completion must not steal focus');
assert.equal(polls, expectedPolls, 'a successful send refreshes only its displayed room');
assert.deepEqual(errors, mode === 'network' ? ['offline']
                        : mode === 'refused' ? ['request refused'] : []);
assert.equal(posts.length, ['out-of-order', 'duplicate-completion'].includes(mode) ? 2 : 1,
             'completion must never re-send a draft');
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the page's send path")
@pytest.mark.parametrize(
    "mode",
    [
        "unchanged",
        "edited",
        "restored",
        "programmatic",
        "other-room",
        "returned-room",
        "out-of-order",
        "duplicate-completion",
        "refused",
        "network",
    ],
)
def test_pending_send_only_clears_its_own_draft_revision(mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", DRAFT_PROBE, mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
