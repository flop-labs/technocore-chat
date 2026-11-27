"""The /humans WebMCP read_note tool must hand back exactly the stored value.

Its description promises the value "can be handed straight back to write_note as `if`", so
any difference from the stored bytes turns the documented read-modify-write into a 409.
Node executes the served tool against the real local app's response.
"""

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client

NODE = shutil.which("node")

READ_NOTE = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const html = input.html;
const slice = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));
const source =
  slice('  function stripNote(', '  function readNote(') +
  slice('  var TOOLS = [', '  function modelContext(');
const context = vm.createContext({
  NAME_RE: /^[a-z0-9][a-z0-9_-]{0,47}$/, encodeURIComponent, JSON, String, Promise,
  fetch: async () => ({ ok: true, status: 200, text: async () => input.body }),
});
vm.runInContext(source + '\nvar readNote = TOOLS.find(t => t.name === "read_note");', context);
context.readNote.run({ ns: 'probe', key: 'value' }).then(out => {
  console.log(JSON.stringify(out.content[0].text));
});
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the served JavaScript")
@pytest.mark.parametrize(
    "value",
    ["step 3 done", "# budget: 9 of 120 reads left this minute"],
    ids=["ordinary", "looks-like-the-footer"],
)
def test_webmcp_read_note_returns_exactly_the_stored_value(client, value):
    assert client.post("/kv/probe/value", json={"value": value}).status_code == 200
    body = client.get("/kv/probe/value").text
    assert body.split("\n\n", 1)[1] == value + "\n"  # banner, then the value; no footer
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", READ_NOTE],
        input=json.dumps({"html": client.get("/humans").text, "body": body}),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == value
