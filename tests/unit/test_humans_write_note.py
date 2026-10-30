"""Execute the page's WebMCP `write_note` tool and post what it sends to the real note lane.

Node runs the tool's own `run` function out of the served page; the body it hands to
`call()` is then posted to `app.py`, so the assertion is on what the server does with the
bytes the page actually builds rather than on a reading of the JavaScript.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

import _client  # noqa: E402

NODE = shutil.which("node")
client = _client.client  # the shared TestClient fixture

PROBE = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync('src/humans.html', 'utf8');
// Locate the tool by its name, then take `run` as a balanced-brace span (skipping string
// literals and comments), so a formatting change inside the function cannot break the
// extraction: only a missing tool or a missing `run` fails here.
const at = html.indexOf("name: 'write_note'");
assert.ok(at >= 0, 'the write_note tool exists');
const start = html.indexOf('run: function', at);
assert.ok(start >= 0, 'the write_note tool has a run function');
const open = html.indexOf('{', start);
let depth = 0, end = -1;
for (let i = open; i < html.length && end < 0; i++) {
  const c = html[i];
  if (c === "'" || c === '"' || c === '`') {
    for (i++; i < html.length && html[i] !== c; i++) if (html[i] === '\\') i++;
  } else if (c === '/' && html[i + 1] === '/') {
    i = html.indexOf('\n', i);
  } else if (c === '/' && html[i + 1] === '*') {
    i = html.indexOf('*/', i + 2) + 1;
  } else if (c === '{') depth++;
  else if (c === '}' && --depth === 0) end = i + 1;
}
assert.ok(end > open, 'the run function closes');
const tool = [null, html.slice(start + 'run: '.length, end)];
const sent = [];
const env = {
  String, JSON, encodeURIComponent,
  allowed: (what, value) => value,
  call: (path, init) => { sent.push({ path, body: JSON.parse(init.body) }); return Promise.resolve('ok'); },
  result: body => body,
};
const run = vm.runInNewContext('(' + tool[1] + ')', env);
for (const input of JSON.parse(process.argv[1])) await run(input);
process.stdout.write(JSON.stringify(sent));
"""


def _sent_by_the_page(inputs: list[dict]) -> list[dict]:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROBE, json.dumps(inputs)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is required to execute the page's tool")
def test_write_note_sends_both_conditions_so_the_server_can_refuse_the_pair(client):
    """#290, on the page's lane: `if_absent` means "nothing is there", `if` means "this exact
    value is there", and there is no correct pick between them — only the server's 400. The
    tool dropped `if` whenever `if_absent` was true, so the contradictory call its own schema
    says "cannot be combined" came back as a successful create on a missing key. The Python
    wrapper stopped doing this in #621; this is the same defect on the other client.
    """
    (both,) = _sent_by_the_page(
        [{"ns": "plans", "key": "new", "value": "v", "if": "current", "if_absent": True}]
    )
    assert both["body"] == {"value": "v", "if_absent": True, "if": "current"}
    refused = client.post("/kv/plans/new", json=both["body"])
    assert refused.status_code == 400, refused.text
    assert "send one condition, not both" in refused.text
    assert client.get("/kv/plans/new").status_code == 404  # nothing was created


@pytest.mark.skipif(NODE is None, reason="node is required to execute the page's tool")
def test_write_note_still_sends_a_single_condition_unchanged(client):
    create, replace, plain = _sent_by_the_page(
        [
            {"ns": "plans", "key": "k", "value": "a", "if_absent": True},
            {"ns": "plans", "key": "k", "value": "b", "if": "a"},
            {"ns": "plans", "key": "k", "value": "c", "if_absent": False},
        ]
    )
    assert create["body"] == {"value": "a", "if_absent": True}
    assert replace["body"] == {"value": "b", "if": "a"}
    assert plain["body"] == {"value": "c"}
    for sent in (create, replace, plain):
        assert client.post("/kv/plans/k", json=sent["body"]).status_code == 200
