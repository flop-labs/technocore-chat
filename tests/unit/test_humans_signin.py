"""Exercise actual identity controls with real Ed25519 and deferred async results.

The credential stand-in supplies PRF bytes, not a real authenticator. Imports use
Node WebCrypto; gates hold their completion without changing what key they produce.
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
function source(name) {
  const match = html.match(new RegExp('  function ' + name + '\\([^\\n]*\\}\\n')) ||
    html.match(new RegExp('  function ' + name + '\\([^]*?\\n  \\}'));
  assert.ok(match, name + ' exists');
  return match[0];
}
const names = ['hexBytes', 'bytesHex', 'b64uBytes', 'base58', 'didOf', 'shortDid',
  'keyFromSeed', 'passkeySeed', 'signIn', 'usePasskey', 'startIdentity', 'load', 'save', 'drop',
  'badge', 'hold', 'say', 'fail'];
const constants = ['PKCS8_ED25519', 'MULTICODEC_ED25519', 'B58', 'SEED_RE',
  'SEED_KEY', 'PRF_SALT', 'CEREMONY_MS', 'CEREMONY_GRACE_MS', 'HOLD_MS'].map(name => {
    const match = html.match(new RegExp('  var ' + name + ' = [^;]+;'));
    assert.ok(match, name + ' exists');
    return match[0];
  });
// Main has no generation counter; run these same assertions there as regressions.
const state = html.match(/  var identityAttempt = [^;]+;/)?.[0] || '';
const begin = html.includes('  function beginIdentity(') ? source('beginIdentity') : '';
const controls = ['keyUseEl', 'keyPassEl', 'keyOutEl'].map(name => {
  const match = html.match(new RegExp('  ' + name +
    "\\.addEventListener\\('click', function \\(\\) \\{[^]*?\\n  \\}\\);"));
  assert.ok(match, name + ' click handler exists');
  return match[0];
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const watchdog = setTimeout(() => { throw new Error('identity operation did not settle'); }, 5000);
const gates = new Map(), storage = new Map(), timers = [], credentials = [], signIns = [];
const clicks = {};
const env = {
  Uint8Array, TextEncoder, atob, AbortController, Date, me: null, ceremony: null,
  holdUntil: 0, status: { textContent: '', className: '' }, seedEl: { value: '' },
  keyMoreEl: { open: false }, identityEl: { hidden: true }, renderIdentity() {},
  localStorage: {
    getItem(k) { return storage.get(k) ?? null; },
    setItem(k, v) { storage.set(k, v); }, removeItem(k) { storage.delete(k); },
  },
  crypto: {
    getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
    subtle: {
      async importKey(...args) {
        const key = await webcrypto.subtle.importKey(...args);
        const gate = gates.get(Buffer.from(args[1]).subarray(-32).toString('hex'));
        if (gate) {
          gate.entered.resolve();
          const error = await gate.release.promise;
          if (error) throw error;
        }
        return key;
      },
      exportKey: webcrypto.subtle.exportKey.bind(webcrypto.subtle),
    },
  },
  navigator: { credentials: {
    get(options) {
      const request = { ...deferred(), signal: options.signal };
      credentials.push(request);
      return request.promise;
    },
  } },
  setTimeout(callback, ms) {
    const timer = { callback, ms, active: true, done: deferred() };
    timers.push(timer);
    return timer;
  },
  clearTimeout(timer) { timer.active = false; timer.done.resolve(); },
};
for (const name of ['keyUseEl', 'keyPassEl', 'keyOutEl']) {
  env[name] = { addEventListener(event, callback) { clicks[name] = callback; } };
}
env.window = { crypto: env.crypto };
vm.createContext(env);
vm.runInContext([...constants, state, begin, ...names.map(source), ...controls].join('\n'), env);
// Observe promises returned by the real function without replacing its behavior.
const signIn = env.signIn;
env.signIn = (...args) => {
  const promise = signIn(...args);
  signIns.push(promise);
  return promise;
};
const A = '11'.repeat(32), B = '22'.repeat(32);
function holdImport(seed) {
  const gate = { entered: deferred(), release: deferred() };
  gates.set(seed, gate);
  return gate;
}
function seed(seedHex) {
  const before = signIns.length;
  env.seedEl.value = seedHex;
  clicks.keyUseEl();
  assert.equal(signIns.length, before + 1);
  assert.equal(env.seedEl.value, '', 'the actual seed control clears its input');
  return signIns.at(-1);
}
function passkey() {
  clicks.keyPassEl();
  const request = credentials.at(-1), timer = timers.at(-1);
  assert.equal(timer.ms, env.CEREMONY_MS + env.CEREMONY_GRACE_MS);
  return { ...request, timer, done: timer.done.promise };
}
function supply(request, seedHex) {
  request.resolve({ getClientExtensionResults() {
    return { prf: { results: { first: new Uint8Array(Buffer.from(seedHex, 'hex')) } } };
  } });
}
function view() {
  return JSON.stringify({ seed: env.me?.seed, provider: env.me?.provider,
    stored: storage.get(env.SEED_KEY), status: env.status, open: env.keyMoreEl.open });
}
async function expectIdentity(seedHex, provider) {
  assert.equal(env.me.seed, seedHex);
  assert.equal(env.me.provider, provider);
  const jwk = await webcrypto.subtle.exportKey('jwk', env.me.key);
  assert.equal(jwk.d, Buffer.from(seedHex, 'hex').toString('base64url'));
  assert.equal(env.me.did, env.didOf(new Uint8Array(Buffer.from(jwk.x, 'base64url'))));
  assert.equal(storage.get(env.SEED_KEY), provider === 'seed' ? seedHex : undefined);
  assert.match(env.status.textContent, /^signed in as /);
}

if (mode === 'current-seed') {
  await seed(A);
  await expectIdentity(A, 'seed');
} else if (mode === 'current-seed-error') {
  const gate = holdImport(A), pending = seed(A);
  await gate.entered.promise;
  gate.release.resolve(new Error('import refused'));
  await pending.catch(() => {});
  assert.equal(env.me, null);
  assert.match(env.status.textContent, /error.*import refused/);
} else if (mode === 'restore-error') {
  storage.set(env.SEED_KEY, A);
  const gate = holdImport(A);
  env.startIdentity();
  await gate.entered.promise;
  const restored = signIns.at(-1);
  await seed(B);
  await expectIdentity(B, 'seed');
  const selected = view();
  gate.release.resolve(new Error('obsolete restoration failed'));
  await restored.catch(() => {});
  assert.equal(view(), selected, 'obsolete restoration must not remove the newer saved seed');
} else if (mode === 'timeout-import') {
  const gate = holdImport(A), request = passkey();
  supply(request, A);
  await gate.entered.promise;
  request.timer.callback();
  assert.equal(env.me, null);
  assert.match(env.status.textContent, /prompt timed out/);
  const timedOut = view();
  gate.release.resolve();
  await request.done;
  assert.equal(view(), timedOut, 'a key imported after the deadline must not sign back in');
  assert.ok(Number.isFinite(env.holdUntil));
} else if (mode === 'current-passkey' || mode === 'current-passkey-error' || mode === 'timeout') {
  storage.set(env.SEED_KEY, B);
  const request = passkey();
  assert.equal(env.holdUntil, Infinity, 'heartbeat remains held during a live ceremony');
  if (mode === 'current-passkey') supply(request, A);
  else if (mode === 'timeout') {
    request.signal.addEventListener('abort', () => request.reject(new DOMException('', 'AbortError')));
    request.timer.callback();
  } else request.reject(new DOMException('', 'NotAllowedError'));
  await request.done;
  if (mode === 'current-passkey') await expectIdentity(A, 'passkey');
  else {
    assert.equal(env.me, null);
    assert.equal(storage.get(env.SEED_KEY), B);
    assert.equal(env.keyMoreEl.open, true, 'current failure opens the alternative controls');
    assert.match(env.status.textContent, mode === 'timeout' ? /prompt timed out/ : /no passkey used/);
  }
  assert.equal(env.ceremony, null);
  assert.ok(Number.isFinite(env.holdUntil), 'settlement releases the infinite hold');
} else if (mode.startsWith('seed-')) {
  const gate = holdImport(A), pending = seed(A);
  await gate.entered.promise;
  if (mode === 'seed-passkey') {
    const request = passkey(); supply(request, B); await request.done;
    await expectIdentity(B, 'passkey');
  } else {
    await seed(B);
    await expectIdentity(B, 'seed');
    if (mode === 'seed-logout') clicks.keyOutEl();
  }
  const selected = view();
  gate.release.resolve(mode === 'seed-error' ? new Error('obsolete import failed') : undefined);
  await pending.catch(() => {});
  assert.equal(view(), selected, 'obsolete key import must not alter identity, storage or status');
} else {
  const importing = mode.startsWith('passkey-import-');
  const gate = importing ? holdImport(A) : null;
  const old = passkey();
  if (gate) { supply(old, A); await gate.entered.promise; }
  if (mode === 'passkey-import-passkey') {
    const next = passkey(), waiting = view();
    gate.release.resolve();
    await old.done;
    assert.equal(view(), waiting, 'old imported key cannot sign in during the next ceremony');
    assert.equal(env.holdUntil, Infinity, 'old settlement must preserve the newer hold');
    supply(next, B); await next.done;
    await expectIdentity(B, 'passkey');
  } else {
    await seed(B);
    await expectIdentity(B, 'seed');
    if (mode === 'passkey-logout') clicks.keyOutEl();
    const selected = view();
    if (gate) gate.release.resolve();
    else if (mode === 'passkey-error') old.reject(new DOMException('', 'NotAllowedError'));
    else supply(old, A);
    await old.done;
    assert.equal(view(), selected, 'obsolete passkey completion must preserve the latest choice');
  }
  assert.equal(old.signal.aborted, true, 'a newer identity choice aborts the old ceremony');
}
assert.ok(timers.every(timer => !timer.active), 'all settled ceremony deadlines are cleared');
clearTimeout(watchdog);
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the identity controls")
@pytest.mark.parametrize(
    "mode",
    [
        "current-seed",
        "current-seed-error",
        "restore-error",
        "current-passkey",
        "current-passkey-error",
        "timeout",
        "timeout-import",
        "seed-success",
        "seed-error",
        "seed-passkey",
        "seed-logout",
        "passkey-success",
        "passkey-error",
        "passkey-logout",
        "passkey-import-seed",
        "passkey-import-passkey",
    ],
)
def test_only_the_latest_identity_choice_takes_effect(mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROBE, mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
