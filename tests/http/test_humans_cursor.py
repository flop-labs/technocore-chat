"""The served room pump must consume a successful empty page's checkpoint too.

Node executes the actual callback; the origin responses come from the real local app.
The full DOM/network interaction is covered by humans_ui_probe.mjs in the browser gate.
"""

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client
NODE = shutil.which("node")

PUMP = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const source = input.html.slice(input.html.indexOf('  function pump()'),
                               input.html.indexOf('  function open('));
const rendered = [], cursors = [];
let turn = 0;
const context = vm.createContext({
  room: 'cursor', since: input.since, targetSeq: input.target, primed: false,
  pollCtl: null, POLL_WAIT: 10, AbortController, Date, encodeURIComponent,
  document: {hidden: false, createElement: () => ({})},
  log: {appendChild: () => {}, querySelector: () =>
    rendered.some(m => m.seq === context.targetSeq) ? {} : null},
  render: messages => rendered.push(...messages),
  restamp: () => {}, beat: () => {}, live: () => {}, later: () => {},
  fail: message => { throw new Error(message); },
  fetch: async path => {
    const since = new URL(path, 'https://test.invalid').searchParams.get('since');
    cursors.push(Number(since));
    const view = input.responses[turn][since];
    if (!view) throw new Error('unexpected cursor: ' + since);
    return {ok: true, json: async () => view};
  },
});
vm.runInContext(source, context);
(async () => {
  for (turn = 0; turn < input.responses.length; turn++) {
    context.pump();
    await new Promise(setImmediate);
  }
  console.log(JSON.stringify({since: context.since, cursors,
                             rendered: rendered.map(m => m.seq)}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the served JavaScript")
@pytest.mark.parametrize(
    "initial,target,expected",
    [(2, 9999, [3]), (0, 9999, [1]), (2, 0, [1, 2, 3]), (2, 2, [2, 3])],
    ids=["future-existing", "future-absent", "ordinary", "valid-permalink"],
)
def test_human_pump_uses_empty_checkpoints_before_the_next_message(
    client, initial, target, expected
):
    for i in range(initial):
        assert (
            client.post("/r/cursor", json={"from": "test", "text": f"initial {i}"}).status_code
            == 200
        )
    since = target - 1 if target else 0
    responses = []
    for turn in range(2):
        responses.append(
            {
                str(cursor): client.get(f"/r/cursor?format=json&since={cursor}").json()
                for cursor in {since, initial}
            }
        )
        if not turn:
            assert (
                client.post("/r/cursor", json={"from": "test", "text": "new message"}).status_code
                == 200
            )
    if target == 9999:
        assert responses[0][str(since)]["messages"] == []
        assert responses[0][str(since)]["last_seq"] == initial
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", PUMP],
        input=json.dumps(
            {
                "html": client.get("/humans").text,
                "since": since,
                "target": target,
                "responses": responses,
            }
        ),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    observed = json.loads(result.stdout)
    assert observed["cursors"] == [since, initial]
    assert observed["since"] == initial + 1
    assert observed["rendered"] == expected
