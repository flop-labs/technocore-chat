"""Run the served WebMCP writer and submit its payload to the real note lane."""

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node executes the page's WebMCP tool")

PROBE = r"""
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {html, input} = JSON.parse(readFileSync(0, 'utf8'));
const source = html.match(/  var TOOLS = \[[\s\S]*?\n  \];/);
if (!source) throw new Error('WebMCP tool definitions not found');
let sent;
const context = {
  input,
  allowed: (field, value) => value,
  result: value => value,
  call: (path, options) => {
    sent = {path, options};
    return Promise.resolve('ok');
  },
};
Promise.resolve(vm.runInNewContext(source[0] +
  "\nTOOLS.find(t => t.name === 'write_note').run(input);", context))
  .then(() => process.stdout.write(JSON.stringify(sent)));
"""


def page_write(client, value, conditional):
    assert NODE is not None
    arguments = {"ns": "plans", "key": "state", "value": value}
    if conditional:
        arguments["if"] = "original"
    run = subprocess.run(
        [NODE, "-e", PROBE],
        input=json.dumps({"html": client.get("/humans").text, "input": arguments}),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert run.returncode == 0, run.stderr
    sent = json.loads(run.stdout)
    return client.request(
        sent["options"]["method"],
        sent["path"],
        content=sent["options"]["body"],
        headers=sent["options"]["headers"],
    )


@pytest.mark.parametrize("conditional", [False, True])
@pytest.mark.parametrize("value", [42, True, ["task"], {"state": "updated"}])
def test_non_string_note_values_cannot_overwrite_existing_state(client, value, conditional):
    path = "/kv/plans/state"
    assert client.post(path, json={"value": "original"}).status_code == 200
    assert client.post(path, json={"value": value}).status_code == 400
    response = page_write(client, value, conditional)
    assert response.status_code == 400, response.text
    assert "bad value: must be a string" in response.text
    assert client.get(path).text.split("\n")[2] == "original"


@pytest.mark.parametrize("conditional", [False, True])
@pytest.mark.parametrize("value", ["42", "true", '["task"]', '{"state":"updated"}'])
def test_string_note_values_still_replace_existing_state(client, value, conditional):
    path = "/kv/plans/state"
    assert client.post(path, json={"value": "original"}).status_code == 200
    response = page_write(client, value, conditional)
    assert response.status_code == 200, response.text
    assert client.get(path).text.split("\n")[2] == value
