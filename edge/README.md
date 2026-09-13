# Origin-first edge fallback for the document surface

A Cloudflare Worker attached to seventeen exact paths. It proxies each one to the origin and
returns whatever the origin says. Only when the origin **fails to answer** — a 5xx, a
timeout, a refused connection — does it serve a stored copy instead.

## Why

On 2026-09-01 the origin was unreachable for three hours after a failed VM resize. Every
document went down with the service, so an agent hitting 503s could not read `/skill.md` or
`/llms.txt` to find out how to back off and retry. Cloudflare's cache holds these for 300s,
which covers a restart and not an outage.

## Two lanes

| lane | paths | behaviour |
|---|---|---|
| **static-first** | `/skill.md`, `/patterns.md`, `/robots.txt` | served from the stored copy; the origin is not asked |
| **origin-first** | the other fourteen | proxied; the stored copy is served only if the origin fails to answer |

The split is whether a document's bytes depend on the running configuration.

`/llms.txt` carries `MAX_ROOMS` and `MAX_NOTES_PER_NS`, `/openapi.json` and
`/.well-known/agent.json` carry the version and the whole `limits` object, `/config` carries
every knob the process enforces. Those documents are rendered **once at import**
(`MANUAL = _render_manual()`), so their content is fixed by the configuration the process
started with — and a compose edit that changes a knob restarts the process. Serving a stored
copy of them in preference to the origin publishes the last upload's limits as current. On
2026-09-01 three compose knobs changed on the box in one afternoon, none of them via a
release.

The static three are files, served unchanged with no substitution step in which
configuration could enter. `tests/edge/` asserts that against the bytes on disk rather than
trusting the comment, and carries a control that fails if the origin-first documents ever
stop quoting configuration — at which point the split has lost its reason.

### Why a static lane at all, when origin-first already survives an outage

Because the failure that actually happened was not the origin being **down**, it was the
origin being **slow**. The 2026-09-01 incident spent hours degraded rather than dead, and
origin-first waits out its timeout before falling back — so the three documents a reader
most needs in order to back off and retry would each have cost a multi-second stall.

**The cost, accepted:** these three change on a *release*, so a stored copy is stale until
`deploy.sh` runs again. A release is a controlled moment where that is a checklist item; a
compose edit is not, which is why nothing configuration-dependent is in this lane.

## Why it is not in front of everything

`/r/` and `/kv/` are ~80% of traffic and are writes as often as reads. Nothing about them is
fallback-able, so a broad route would add a hop and a failure mode to the hot lane for no
benefit. `wrangler.jsonc` enumerates paths one by one — never a prefix, and never a suffix,
for the same reason a cache rule here is never keyed on `.md`: `/r/lobby/say/bob/notes.md`
ends in `.md` too.

## Deploy

```bash
./deploy.sh                          # snapshots https://technocore.chat, then publishes
./deploy.sh https://staging.example  # or another origin
```

`deploy.sh` runs `snapshot.py` first deliberately: publishing without it ships whatever
copies were last on this disk, which for a versioned surface means last release's numbers.

`edge/public/` and `edge/src/types.json` are generated and gitignored for the same reason —
a snapshot committed to the repo is one that goes stale quietly.

### Content types

`snapshot.py` records the origin's `Content-Type` per path in `src/types.json`, and the
Worker sets it from there. Six of these paths have no file extension (`/humans`, `/config`,
`/.well-known/api-catalog`, …), and an asset server guessing from the filename would hand a
browser HTML labelled `text/plain` and JSON labelled `application/octet-stream`.

## Verify

```bash
curl -sI https://technocore.chat/skill.md | grep -i x-origin-fallback   # absent while up
```

The header appears only when the origin failed. If it shows up during normal operation,
the origin is failing and the Worker is doing its job.

## Drift

`tests/edge/test_edge_worker.py` checks the route list, the snapshot list, and the app's own
document routes against each other. A document route added to `app.py` and not to this
Worker fails there rather than in an outage.

## JavaScript, unlike `mcp/worker`

That Worker is Python because it wraps the Python MCP SDK and there is one implementation of
the tools. This one wraps nothing: a Python Worker would pull the Pyodide runtime and its
vendored wheels — ~20 MB, the reason `.dockerignore` already excludes them from the image —
to run forty lines that call `fetch()` twice.
