"""Trimming the live log must preserve the retained history being read.

Execute the actual renderer without native scroll anchoring, which Chromium suppresses
when the new rows animate. Browser reproduction also covers reduced-motion anchoring.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[2]
PROBE = r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const html = require('node:fs').readFileSync('src/humans.html', 'utf8');
const source = html.slice(html.indexOf('  function render(messages)'),
                          html.indexOf('  // ------------------------------------------------------------------------- the pump'));
const mode = process.argv[1];
class Element {
  constructor(height = 31.5) { this.height = height; this.children = []; this.dataset = {}; }
  appendChild(child) { this.children.push(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener() {}
  querySelector() { return null; }
  get firstChild() { return this.children[0]; }
  getBoundingClientRect() { return {height: this.height}; }
}
const log = new Element();
log.clientHeight = 180;
Object.defineProperty(log, 'scrollHeight', {
  get: () => log.children.reduce((sum, row) => sum + row.height, 0),
});
for (let i = 0; i < (mode === 'uncapped' ? 195 : 200); i++)
  log.appendChild(new Element(20.25 + i % 3 * 10));
log.scrollTop = mode === 'bottom' ? log.scrollHeight - log.clientHeight
  : mode === 'expired' ? 25 : 1000;
const target = log.children[100];
const position = () => log.children.slice(0, log.children.indexOf(target))
  .reduce((sum, row) => sum + row.height, 0) - log.scrollTop;
const before = position(), context = vm.createContext({
  log, room: 'cap', targetSeq: 0, targetRevealed: false, firstPaint: false,
  MAX_ROWS: 200, unseen: 0,
  atBottom: () => log.scrollHeight - log.scrollTop - log.clientHeight < 40,
  document: {createElement: () => new Element()},
  copyButton: () => new Element(), permalink: () => '', setHash() {}, stamp() {},
  renderJump() {}, toBottom: () => { log.scrollTop = log.scrollHeight - log.clientHeight; },
});
vm.runInContext(source, context);
context.render(Array.from({length: 5}, (_, i) => ({seq: 201 + i, from: 'bot', text: 'new'})));
assert.equal(log.children.length, 200, 'the row bound still holds');
assert.ok(log.children.includes(target), 'the history under test remains retained');
if (mode === 'bottom') {
  assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight, 'latest still follows arrivals');
} else if (mode === 'expired') {
  assert.equal(log.scrollTop, 0, 'a removed reading position clamps to the oldest retained row');
} else {
  assert.equal(position(), before, 'retained history must stay at the same viewport position');
}
assert.equal(context.unseen, mode === 'bottom' ? 0 : 5);
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the renderer")
@pytest.mark.parametrize("mode", ["history", "expired", "bottom", "uncapped"])
def test_row_cap_preserves_the_reading_position(mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", PROBE, mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
