"""The room table's 5-second rebuild must not take keyboard focus away from the reader.

Node executes the served renderRooms against a small DOM spy; the Chromium probe checks the
same thing with real focus and Tab. This adds no Python or browser dependency.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client

RUN_RENDER = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const doc = { activeElement: null };
const focusCalls = [];
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
    this.dataset = {}; this.className = '';
  }
  set textContent(value) {
    this.children.forEach((c) => { c.parent = null; });
    this.children = []; this.text = value;
  }
  get textContent() { return this.text || ''; }
  get firstChild() { return this.children[0] || null; }
  get classList() { return { contains: (c) => this.className.split(' ').includes(c) }; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  addEventListener() {}
  contains(node) {
    for (let n = node; n; n = n.parent) if (n === this) return true;
    return false;
  }
  closest(tag) {
    for (let n = this; n; n = n.parent) if (n.tagName === tag.toUpperCase()) return n;
    return null;
  }
  focus(options) { focusCalls.push(options || null); doc.activeElement = this; }
}
const roomsBody = new El('tbody'), filter = new El('input');
filter.value = '';
doc.createElement = (tag) => new El(tag);
const context = {
  document: doc, roomsBody, filterEl: filter, roomsView: null,
  renderStats() {}, matches() { return true; }, open() {}, revealRoom() {},
  size: (n) => n + 'B', ago: (s) => s + 's', permalink: () => '',
  iconCopy(btn, label, value, className) { btn.className = className; return btn; },
};
vm.createContext(context);
vm.runInContext(input.script, context);
const view = (names) => ({
  total: names.length,
  rooms: names.map((room) => ({ room, topic: '', last_seq: 1, bytes: 1, idle_seconds: 1 })),
});
const render = (names) => { context.roomsView = view(names); vm.runInContext('renderRooms()', context); };
// Rows are found by the room button's own label, which the page has always rendered.
const roomOf = (tr) => tr.children[0].children[0].textContent;
const row = (room) => roomsBody.children.find((tr) => roomOf(tr) === room);
const describe = (el) => {
  const tr = el && el.parent ? el.closest('tr') : null;
  if (!tr || !roomsBody.contains(el)) return el === filter ? 'filter' : 'detached';
  return roomOf(tr) + (el.classList.contains('icon-btn') ? ' copy' : ' open');
};
const out = {};

render(['a', 'b', 'c']);
row('b').children[0].children[0].focus();
focusCalls.length = 0;
render(['c', 'b', 'a']);
out.open = { focused: describe(doc.activeElement), calls: focusCalls.slice() };

row('b').children[4].firstChild.focus();
focusCalls.length = 0;
render(['b', 'a', 'c']);
out.copy = { focused: describe(doc.activeElement), calls: focusCalls.slice() };

focusCalls.length = 0;
render(['a', 'c']);
out.gone = { focused: describe(doc.activeElement), calls: focusCalls.slice() };

filter.focus();
focusCalls.length = 0;
render(['a', 'b', 'c']);
out.outside = { focused: describe(doc.activeElement), calls: focusCalls.slice() };

process.stdout.write(JSON.stringify(out));
"""


def test_room_list_refresh_keeps_keyboard_focus(client):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; humans_ui_probe.mjs covers this in Chromium")
    page = client.get("/humans").text
    # The served renderRooms itself, never a reimplementation of its focus handling.
    start = page.index("  function renderRooms() {")
    end = page.index("  // Every value below is inserted with textContent", start)
    output = subprocess.run(
        [node, "-e", RUN_RENDER],
        input=json.dumps({"script": page[start:end]}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    result = json.loads(output.stdout)
    no_scroll = [{"preventScroll": True}]
    # Focus follows the room, not the row position, and never scrolls the page.
    assert result["open"] == {"focused": "b open", "calls": no_scroll}, result
    assert result["copy"] == {"focused": "b copy", "calls": no_scroll}, result
    # A room that left the list has nothing to hand focus back to.
    assert result["gone"]["calls"] == [], result
    # Focus outside the table is not the table's to move.
    assert result["outside"] == {"focused": "filter", "calls": []}, result
