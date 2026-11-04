"""Exercise overlapping directory refreshes with deferred response bodies, without sleeps."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")

PROBE = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const mode = process.argv[1];
const html = readFileSync('src/humans.html', 'utf8');
const source = html.slice(html.indexOf('  var ROOMS_DEADLINE_MS'),
                          html.indexOf('  function roomsProblem('));
const requests = [], rendered = [], timers = new Set();
let failures = 0;
const context = vm.createContext({
  AbortController, roomsView: null,
  setTimeout: fn => { timers.add(fn); return fn; },
  clearTimeout: fn => timers.delete(fn),
  fetch: () => new Promise(resolve => {
    // Headers have arrived; the body can still take longer than the next refresh.
    const body = new Promise((done, reject) => requests.push({done, reject}));
    resolve({json: () => body});
  }),
  renderRooms: () => rendered.push(context.roomsView),
  roomsProblem: () => failures++,
});
vm.runInContext(source, context);
const flush = () => new Promise(resolve => setImmediate(resolve));
const older = {rooms: [{room: 'old-room'}]}, newer = {rooms: [{room: 'new-room'}]};
context.loadRooms();
context.loadRooms();
assert.equal(requests.length, 2);
if (mode === 'reverse') {
  requests[1].done(newer);
  await flush();
  requests[0].done(older);
  await flush();
  assert.deepEqual(rendered, [newer], 'a late old response must not replace the newer list');
  assert.equal(context.roomsView, newer, 'filtering must keep using the newer snapshot');
} else if (mode === 'ordered') {
  requests[0].done(older);
  await flush();
  assert.deepEqual(rendered, [older], 'show an available result while a newer one is pending');
  requests[1].done(newer);
  await flush();
  assert.deepEqual(rendered, [older, newer]);
} else if (mode === 'newer-fails') {
  requests[1].reject(new Error('timeout'));
  await flush();
  requests[0].done(older);
  await flush();
  assert.deepEqual(rendered, [older], 'a failed newer request must not discard usable data');
  assert.equal(failures, 1);
} else if (mode === 'older-fails') {
  requests[1].done(newer);
  await flush();
  requests[0].reject(new Error('timeout'));
  await flush();
  assert.deepEqual(rendered, [newer]);
  assert.equal(failures, 0, 'an old failure must not erase a loaded list');
}
assert.equal(timers.size, 0, 'every settled request releases its deadline');
// Discarding an old response must not stop subsequent refreshes.
context.loadRooms();
requests[2].done({rooms: []});
await flush();
assert.deepEqual(rendered.at(-1), {rooms: []});
assert.equal(timers.size, 0);
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the directory refresh")
@pytest.mark.parametrize("mode", ["reverse", "ordered", "newer-fails", "older-fails"])
def test_directory_refresh_does_not_replace_a_newer_success(mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROBE, mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
