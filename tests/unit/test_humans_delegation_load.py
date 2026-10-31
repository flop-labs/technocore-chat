"""Run the page's delegation reader with real signatures and out-of-order responses.

Node supplies WebCrypto. Deferred fetches make the races deterministic; the browser
probe separately exercises identity controls and the rendered delegation list.
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
const html = readFileSync('src/humans.html', 'utf8');
const names = ['hexBytes', 'bytesHex', 'b64uBytes', 'bytesB64u', 'base58', 'didOf',
  'unbase58', 'didPublicKey', 'notePath', 'delegationString', 'verifyDelegation',
  'stripNote', 'readNote', 'parseDelegations', 'newest', 'loadDelegations'];
const functions = names.map(name => {
  const match = html.match(new RegExp('  function ' + name + '\\([^]*?\\n  \\}'));
  assert.ok(match, name + ' exists');
  return match[0];
});
const constants = ['B58', 'MULTICODEC_ED25519', 'DELEGATE_TOKEN', 'DELEGATE_FIELDS',
  'DIGITS_RE', 'DID_RE'].map(name => {
    const match = html.match(new RegExp('  var ' + name + ' = [^\\n]+'));
    assert.ok(match, name + ' exists');
    return match[0];
  });
// Include the page's state declaration when present, so the same probe runs on main.
const loadState = html.match(/  var delegationLoad = [^\n]+/)?.[0] || '';
const watchdog = setTimeout(() => { throw new Error('delegation load did not settle'); }, 5000);

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const observed = [deferred(), deferred()], requests = [], renders = [], errors = [];
const env = {
  crypto: webcrypto, TextEncoder, Uint8Array, atob, btoa, Date, me: null,
  renderDelegations(rows) { renders.push(JSON.parse(JSON.stringify(rows))); },
  fail(message) { errors.push(message); },
  fetch(url) {
    const request = { url, ...deferred() };
    requests.push(request);
    observed[requests.length - 1].resolve(request);
    return request.promise;
  },
};
vm.createContext(env);
vm.runInContext([...constants, loadState, ...functions].join('\n'), env);

async function identity() {
  const pair = await webcrypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const raw = await webcrypto.subtle.exportKey('raw', pair.publicKey);
  return { did: env.didOf(new Uint8Array(raw)), key: pair.privateKey };
}
const [original, replacement, agent] = await Promise.all([identity(), identity(), identity()]);
async function note(owner, scope, nonce) {
  const expires = String(Math.floor(Date.now() / 1000) + 86400);
  const canonical = env.delegationString(owner.did, agent.did, scope, expires, nonce);
  const signature = await webcrypto.subtle.sign('Ed25519', owner.key,
                                                new TextEncoder().encode(canonical));
  return 'BANNER\n\ndelegate: ' + [agent.did, scope, expires, nonce,
                                        env.bytesB64u(signature)].join(' ');
}
function expectScope(scope) {
  const latest = renders.at(-1);
  assert.equal(latest.length, 1, 'one delegation is rendered');
  assert.equal(latest[0].state, 'live', 'the real signature verifies');
  assert.equal(latest[0].d.scope, scope);
}

env.me = original;
const old = env.loadDelegations();
const first = await observed[0].promise;
const path = await env.notePath(original.did);
assert.equal(first.url, '/kv/' + path.ns + '/' + path.key);

if (mode.startsWith('current-')) {
  if (mode === 'current-success') {
    first.resolve(new Response(await note(original, 'r:current', '1')));
    await old;
    expectScope('r:current');
    assert.deepEqual(errors, []);
  } else {
    const status = mode === 'current-error' ? 503 : 404;
    first.resolve(new Response('unavailable', { status }));
    await old;
    assert.deepEqual(renders, [[]]);
    if (status === 503) {
      assert.equal(errors.length, 1);
      assert.match(errors[0], /could not read delegations.*HTTP 503/);
    } else assert.deepEqual(errors, []);
  }
} else {
  const [change, result] = mode.split('-');
  if (change === 'switch' || change === 'refresh') {
    env.me = change === 'switch' ? replacement : original;
    const newer = env.loadDelegations();
    const second = await observed[1].promise;
    second.resolve(new Response(await note(env.me, 'r:new', '2')));
    await newer;
    expectScope('r:new');
  } else {
    // A new sign-in object can contain the same DID; it still owns a new view.
    env.me = change === 'logout' ? null : { ...original };
  }
  const selected = env.me;
  const before = JSON.stringify(renders);
  first.resolve(result === 'error' ? new Response('unavailable', { status: 503 })
                                  : new Response(await note(original, 'r:old', '1')));
  await old;
  assert.equal(JSON.stringify(renders), before, 'a stale response must not render');
  assert.deepEqual(errors, [], 'a stale failure must not report an error');
  assert.equal(env.me, selected, 'completion must not change the selected identity');
}
clearTimeout(watchdog);
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the delegation reader")
@pytest.mark.parametrize(
    "mode",
    [
        "current-success",
        "current-error",
        "current-missing",
        "switch-success",
        "switch-error",
        "refresh-success",
        "refresh-error",
        "logout-success",
        "logout-error",
        "reentry-success",
        "reentry-error",
    ],
)
def test_only_the_current_delegation_load_updates_the_view(mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROBE, mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
