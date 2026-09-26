"""IME confirmation must not trigger the page's Enter shortcuts.

Execute the served handlers where Node is available; the separate Chromium probe also
checks the real composer and navigation. This adds no Python or browser dependency.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client

RUN_HANDLERS = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const results = [];
for (const field of ['text', 'room', 'filter']) {
  for (const { event, submit } of input.events) {
    const listeners = {}, actions = [];
    const element = (name) => ({
      value: 'standup',
      addEventListener(type, callback) { listeners[name + ':' + type] = callback; },
    });
    const elements = Object.fromEntries(
      ['text', 'room', 'filter', 'send', 'join'].map((name) => [name, element(name)])
    );
    vm.runInNewContext(input.script, {
      textEl: elements.text, roomEl: elements.room, filterEl: elements.filter,
      document: { getElementById: (name) => elements[name] },
      roomsBody: { querySelector: () => ({ click: () => actions.push('filter') }) },
      renderRooms() {},
      send: () => actions.push('text'),
      open: () => actions.push('room'),
    });
    listeners[field + ':keydown'](event);
    results.push({ field, event, submit, actions });
  }
}
process.stdout.write(JSON.stringify(results));
"""


def test_enter_shortcuts_wait_for_ime_confirmation(client):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; humans_ui_probe.mjs covers this in Chromium")
    page = client.get("/humans").text
    # Include the actual registrations and their shared predicate, never a reimplementation
    # of the condition. Surrounding page setup is unrelated to keyboard-event dispatch.
    start = page.index("  // Re-render on every keystroke:")
    end = page.index("  // Pasting a #room link", start)
    events = [
        {"event": {"key": "Enter", "isComposing": True, "keyCode": 13}, "submit": False},
        {"event": {"key": "Enter", "isComposing": False, "keyCode": 229}, "submit": False},
        {"event": {"key": "Enter", "isComposing": False, "keyCode": 13}, "submit": True},
        {"event": {"key": "Enter", "shiftKey": True, "keyCode": 13}, "submit": True},
        {"event": {"key": "Escape", "isComposing": False, "keyCode": 27}, "submit": False},
    ]
    output = subprocess.run(
        [node, "-e", RUN_HANDLERS],
        input=json.dumps({"script": page[start:end], "events": events}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    for result in json.loads(output.stdout):
        expected = [result["field"]] if result["submit"] else []
        assert result["actions"] == expected, result
