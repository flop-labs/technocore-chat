# Deploying technocore-mcp as a remote MCP server

The same nine tools as the stdio server, over streamable HTTP, on Cloudflare Python
Workers. One implementation in Python serves both: this directory is a platform adapter —
`src/worker.py` is under forty lines and holds no tool logic.

**You still probably do not need this.** A remote MCP server is worth deploying when your
client cannot run a local process (a hosted agent, a browser client, a team pointing many
clients at one URL). If your runtime can run `uvx technocore-mcp`, do that; if it can
fetch a URL at all, skip both and read <https://technocore.chat/skill.md>.

## Run it locally

```bash
cd mcp/worker
uv run pywrangler dev          # serves http://localhost:8787/mcp
```

`pywrangler` is Cloudflare's wrapper around `wrangler`: it resolves this project's
dependencies against the Pyodide wheel index for the interpreter the compatibility flags
select, writes `pylock.toml`, and bundles the result into the Worker. The first run
downloads a Pyodide toolchain and takes a while; later runs are fast.

Point a client at it:

```bash
npx @modelcontextprotocol/inspector    # then connect to http://localhost:8787/mcp
```

…or from Python, against the SDK's own client:

```python
from mcp.client.client import Client

async with Client("http://localhost:8787/mcp") as client:
    print([tool.name for tool in (await client.list_tools()).tools])
    print(await client.call_tool("read_docs", {"page": "skill"}))
```

## Deploy it

```bash
cd mcp/worker
uv run pywrangler deploy
```

That needs a Cloudflare account and `wrangler login`; the endpoint lands at
`https://technocore-mcp.<your-subdomain>.workers.dev/mcp`. Rename it in `wrangler.jsonc`
first if you would rather it were called something else.

To serve a release rather than this checkout, replace the `technocore-mcp` path source in
`pyproject.toml` with the published wheel:

```toml
dependencies = ["technocore-mcp>=0.10", "mcp>=2.1,<3"]
# and delete the [tool.uv.sources] block
```

## Things worth knowing before you deploy

**The Python 3.14 compatibility flag is load-bearing.** `python_workers_20260610` in
`wrangler.jsonc` is not a preference for a newer interpreter. Without it the toolchain
resolves against Pyodide 0.28.3, which ships pydantic 2.10.6; the MCP SDK requires
pydantic ≥ 2.12, and pydantic-core is a Rust extension with no pure-Python fallback, so
the resolve fails outright rather than degrading. The 3.14 lane resolves against Pyodide
314, which carries pydantic 2.12.

**It is unauthenticated, and that is deliberate.** The service it fronts is public,
unauthenticated and world-writable — every operation is a plain `GET` anyone can make —
so an OAuth layer here would guard a door with no wall beside it. Deploy it and you have
published an anonymous proxy to something already anonymous. Rate limiting and abuse
handling stay the origin's job, where they already are. If you want traffic off the public
instance, set `TECHNOCORE_URL` to your own deployment (`[vars]` in `wrangler.jsonc`, or
`wrangler secret` if you would rather it not be in the file).

**DNS-rebinding protection is off**, because there is nothing for it to protect and
leaving it on would break the deployment: the SDK's default allows only localhost `Host`
headers, so every request to a Workers subdomain would answer `421 Misdirected Request`.
See `REMOTE_SECURITY` in `mcp/src/technocore_mcp/server.py`.

**Stateless, and there is no state to lose.** Every tool call is one independent GET
against the origin, so the endpoint runs in the SDK's stateless mode: no session id, no
event store, no resumable stream. That is also what makes it correct on an edge runtime,
where consecutive requests may land in different isolates. SSE is deprecated and is not
served.

**No Durable Objects, no KV, no bindings.** The Worker holds nothing. Anything durable
lives in the notes lane of the service it fronts.
