# fixtures/stats.json

Captured from a real service, not hand-written, so the mapping is tested against bytes the
origin actually produced.

Built on `20a4457` with `CHAT_STATS_CACHE_SECONDS=0` (the default 60s cache otherwise
serves the empty digest taken before the writes landed — which is worth knowing, and is why
`technocore_stats_sample_age_seconds` exists):

```
uv run uvicorn --app-dir src app:app --port 8099     # CHAT_STATS_TOKEN set
GET /r/lobby/say/prober/<text>                x5     # an open room
GET /r/d-probeownable/say/prober/...                 # ownable
GET /r/e-probeeph/say/prober/...                     # ephemeral
GET /r/p-probeprivate/say/prober/...                 # unlisted, no other marker
GET /r/mb-p-probemailbox/say-signed/...              # mailbox AND unlisted (signed: mb- refuses unsigned)
GET /kv/probe-ns/key-{one,two}/set/<value>           # notes
GET /kv/topic/{lobby,e-probeeph}/set/<text>          # topic notes
GET /stats                                           # captured here
```

`/r/events` is in the totals because the service creates it itself to announce room
creations; that is real behaviour and belongs in the fixture.

The two properties the tests lean on, from these exact numbers:

- `listed 4 + unlisted 2 = 6 = total` — a real partition.
- `open 2 + mailbox 1 + ownable 1 + ephemeral 1 = 5 != 6` — not a partition. The mailbox
  room counts under both `mailbox` and `unlisted`, and `p-probeprivate` carries no class
  marker that any of the four names, so the class counts fall short of the total.
