# technocore-exporter

Prometheus exporter for a technocore-chat deployment. It reads the token-gated `/stats`
digest over HTTP and republishes the shared-storage aggregates as metrics. It consumes the
published HTTP surface and ships nothing back into the service — the same relationship
`mcp/` has to `src/` — which is why it is its own package rather than a route on the origin.

`/stats` is unchanged by this package, and keeps serving its existing JSON digest and
history to whatever reads it today.

## Run it

```bash
export TECHNOCORE_STATS_URL=http://127.0.0.1:8080/stats
export TECHNOCORE_STATS_TOKEN=...        # must match the service's CHAT_STATS_TOKEN
uv run --project exporter technocore-exporter
```

`/metrics` binds to `127.0.0.1:9464` by default. **Keep it there or on the operator's own
network.** The digest is token-gated at the origin; `/metrics` is not gated at all, and it
is the same numbers.

It is *only* those numbers. The exporter serves its own `CollectorRegistry`, not the client
library's global one, so the page carries the families listed below and nothing else — no
`python_info`, no `process_*`, no GC series. That is asserted on a rendered page in
`tests/exporter/test_server.py`, so an upstream release adding default collectors cannot
quietly widen this endpoint.

| Variable | Default | |
|---|---|---|
| `TECHNOCORE_STATS_TOKEN` | — | required; read from the environment only, never a flag, because argv is world-readable via `ps` |
| `TECHNOCORE_STATS_URL` | `http://127.0.0.1:8080/stats` | |
| `TECHNOCORE_EXPORTER_HOST` | `127.0.0.1` | |
| `TECHNOCORE_EXPORTER_PORT` | `9464` | |
| `TECHNOCORE_STATS_TIMEOUT` | `5` | seconds; must be finite and `0 < t < 10` — **enforced at boot**, not described. The budget for the whole answer, not one socket operation: see below |

### The timeout is a budget for the answer, not a socket option

`TECHNOCORE_STATS_TIMEOUT` bounds the whole scrape — name resolution, connect, headers,
body, and any time queued behind another scrape — so `/metrics` answers within it plus the
exporter's own assembly, which is milliseconds. That is what puts the ceiling below
Prometheus's own default `scrape_timeout`, so a slow origin reports
`technocore_exporter_scrape_success 0` while Prometheus is still listening, rather than
timing out the scrape and storing nothing — which would say nothing about which side is
unwell.

It reads as an obvious property and has now been false in **five** separate ways, none of
which this package's own tests caught. Two were reported directly by reviewers (@Minh3132,
@yukkie3276); the other three came out of auditing the phase boundary each report exposed.
Every one is now pinned by a regression. They share a cause worth
stating plainly: `urlopen(timeout=)` is a bound on one socket operation, and a scrape is
made of many.

- `urlopen(timeout=)` bounds each socket operation, not the call, so an origin trickling a
  byte at a time under the timeout never tripped it — one fetch took **20.01s** on the 5s
  default and the `GET /metrics` around it answered after **20.18s** reporting
  `scrape_success 1`, with no concurrency involved. `fetch.py` now reads the body against a
  deadline.
- Scrapes serialise on one lock, and the wait was time the timeout never saw: a queued
  scrape spent a full timeout waiting and another on its own fetch — **17.82s for a 9s
  timeout**, past a 15s `scrape_timeout`. `collector.py` now spends one deadline on the
  wait and the fetch together, and publishes the wait in
  `technocore_exporter_scrape_duration_seconds`, which previously reported 9.01s for that
  same 17.82s scrape.
- Name resolution runs before there is a socket for the timeout to apply to, so a wedged
  resolver blocked past it: **20.0s of a 3s budget** at `/metrics` — the scrape
  answered when the resolver did, which is to say the budget bounded nothing.
- Connect then tries *every* address the name resolved to, giving each the full timeout, so
  the budget was spent once per address record rather than once per scrape: **12.0s of a
  3s budget** across four blackholed A records — four times the budget, with each connect
  logged at the whole 3.0s rather than a share. Two records is enough to matter, provided both *blackhole* rather
  than refuse — a refused connection returns at once and costs nothing, while a firewall
  that drops does not. At the ceiling of just under 10s, two dropping addresses reach ~20s
  against a 15s `scrape_timeout`.
- The status line and response headers are read before `urlopen` returns, so the deadline
  in `_read_within` never saw them. An origin trickling one header byte per 0.5s ran to
  **22.6s of a 3s budget**, and the exporter was still waiting when the origin stopped
  trickling — so that figure is the origin's choice, not a bound. This one needs no hostname: it lands on the shipped `127.0.0.1` default,
  because it is about what the origin sends rather than how it is addressed.

The first two fixes bounded the phases this package could see. The last three are phases it
could not, which is why the bound moved up a level: `TechnocoreCollector._fetch_within` runs
the fetch on a worker and stops waiting at the deadline, whatever the transport is doing.
The worker keeps the fetch lock when it is abandoned, so a wedged origin costs one live
thread at a time and one in-flight request, no matter how many scrapes arrive.

### Configuration is validated at boot, not at first scrape

Every setting above is checked before the server starts, and an unusable one refuses to
boot rather than being accepted. That is not tidiness: `collect()` converts any unexpected
error into `scrape_success 0`, so a timeout of `-1`, `nan` or `inf` — each of which raises
from `socket.settimeout` on *every* request — would otherwise produce a process that
starts, stays up, answers `/metrics` forever and never once succeeds. Core takes the same
position for the same reason; see `config._finite_env`.

The same rule covers the rest, and each of these was once accepted at boot: a port out of
range, a URL whose scheme `urllib` cannot open, a URL whose **port** it cannot
(`http://host:notaport/stats` booted and then failed every scrape), a URL malformed enough
that `urlparse` itself raises (`http://[::1/stats` exited with a traceback rather than a
refusal), and a token that is only whitespace (truthy, so it booted and took 404 forever).
Every one now refuses with a message naming the variable. The token's value is *not*
trimmed, only tested — core compares `CHAT_STATS_TOKEN` unstripped, so a token whose
surrounding whitespace is real has to be sent exactly as configured.

## Metrics

Gauges — current state:

| Metric | |
|---|---|
| `technocore_rooms` | rooms that exist, including unlisted; what the room cap bounds |
| `technocore_rooms_capacity` | `store.MAX_ROOMS` |
| `technocore_rooms_listing{state="listed"\|"unlisted"}` | **a true partition** — sums to `technocore_rooms` |
| `technocore_rooms_class{class="mailbox"\|"ownable"\|"ephemeral"}` | **overlapping — never sum** |
| `technocore_rooms_unclassified` | rooms carrying no class marker |
| `technocore_room_bytes` / `technocore_room_bytes_capacity` | disk held by rooms, and the enforced budget |
| `technocore_notes` / `technocore_notes_capacity` | notes across all namespaces |
| `technocore_notes_capacity_per_namespace` | a namespace can hit this while the global count is far from its cap |
| `technocore_note_bytes` | |
| `technocore_stats_sample_age_seconds` | age of the newest **stored sample**, not of the gauges above. Sampling is driven by writes, so this grows without bound on an idle service — context, **not** an availability signal |

Counters — lifetime, best effort:

`technocore_messages_total`, `technocore_rooms_created_total`,
`technocore_rooms_reaped_total{reason="idle"|"stillborn"}`, `technocore_notes_written_total`,
`technocore_topics_written_total`.

Best effort on two axes, and the HELP text says so: they are **batched per worker** and
flushed on an interval or at shutdown, so a hard kill loses the tail; and the counter file
is rebuilt as zeros if lost, which Prometheus reads as a counter reset — correct handling,
but `increase()` across that window undercounts rather than showing a gap.

Exporter self-metrics: `technocore_exporter_scrape_success`,
`technocore_exporter_scrape_duration_seconds`,
`technocore_exporter_last_success_timestamp_seconds`,
`technocore_exporter_scrapes_total{outcome="success"|"error"}`. Without them a wrong token
and a service with no rooms are the same absence of samples.

#### These do not replace Prometheus's own `up`

Alert on both, and on `up` first. Prometheus synthesizes `up` per scrape, and it answers a
question the exporter structurally cannot: whether the page arrived at all. Measured
against Prometheus 3.5.0:

| what happened | `up` | `technocore_exporter_scrape_success` |
|---|---|---|
| exporter answers slower than `scrape_timeout` | `0` | no samples stored |
| exporter is gone | `0` | no samples stored |
| exporter answering, `/stats` read failing | `1` | `0` |

So `up` covers the whole "we never got the page" half, including the slow case — an
exporter that eventually answers past the scrape timeout stores nothing of its own no
matter what it believed it was reporting. The self-metrics uniquely carry the last row,
where the exporter is healthy and the read behind it is not; a wrong `CHAT_STATS_TOKEN` is
the common cause and `/stats` answers it with 404, so nothing about it looks like an
authentication failure. `config/technocore-alerts.yml` ships one alert for each half.

### Why the room classes are two different shapes

`listed`/`unlisted` partition the room population, so they are label values on one metric
and summing them is correct. The class markers do not partition anything: `room_classes`
composes by prefix, so `mb-p-x` is a mailbox *and* unlisted, and a room can carry several
markers at once. Those are separate metric names — a shape nobody is tempted to add up.
The test fixture is a real capture in which the class counts genuinely fall short of the
total, so this is pinned by arithmetic rather than by a comment.

### The token goes to the configured origin and nowhere else

**Ambient proxy configuration is ignored.** `HTTP_PROXY` / `HTTPS_PROXY` in the process
environment would otherwise route this request — token included — through the proxy rather
than to the configured origin, so proxy discovery is disabled outright. That is the right
default for a credential-bearing client whose origin defaults to `127.0.0.1`: proxying
localhost is almost never intended and the `NO_PROXY` exemption for it is easy to get
wrong. An operator who genuinely needs an egress proxy needs an explicit trusted setting,
not an environment variable that happens to be inherited.

Redirects are refused rather than followed. `urllib.request.HTTPRedirectHandler` copies
every header except `content-length` and `content-type` onto a redirected request, so a
302 from the configured URL to any host would hand `X-Stats-Token` to that host silently.
A 3xx is reported as a failed scrape instead.

### A partial digest is refused, not mapped

Every field the mapping reads must be present and a finite, non-negative, non-boolean
number. Mapping what is there and defaulting the rest would publish `technocore_notes 0`
beside `scrape_success 1` — a healthy-looking empty service, against which a note-capacity
alert can never fire. Absent is not zero.

### What this deliberately does not export

- **`requests`** — per *worker*, not per service. `_requests` is a plain module dict, and
  `src/app.py:1914` records the digest under-reporting by 3x once production moved to
  `--workers 3`. Consecutive scrapes of a load-balanced URL can land on different
  processes, so no arrangement of those samples is a monotonic counter, and multiplying by
  `workers` is an estimate rather than counter aggregation. Capture admission failures at
  the HTTP server or proxy instead.
- **`history`** — Prometheus stores what it scrapes; re-exporting the ring as
  timestamp-labelled series would double-store it and produce a second, disagreeing
  history. Only the newest sample's timestamp is used, as freshness.
- **`engagement`** — pooled over the 50 most recently active rooms, so it is a
  bounded-window sample rather than a service total. Beside real totals it invites an alert
  on a number that does not mean what its neighbours mean.
- **Room names, namespaces, DIDs, IP addresses, raw URLs** — never, in any label. An
  unlisted room name and a note namespace are bearer credentials. A test asserts the label
  set as an allowlist, because a denylist passes for whatever nobody thought of.

## Scrape interval

**60s**, as shipped in `config/prometheus-scrape.yml`. The service caches the digest for
`CHAT_STATS_CACHE_SECONDS` (60 by default) because building it is an O(cap) walk. Scraping
faster buys no freshness — it only spends requests — and on a large store the walk behind a
cache miss is the expensive thing monitoring must not trigger repeatedly.

## Checks

```bash
uv run pytest tests/exporter -q

# The exporter serves /metrics; it prints nothing. Scrape it from a second shell — the
# recipe here used to redirect the process's stdout, which is empty, and then hand
# `promtool check metrics` an empty file that it passes.
uv run --project exporter technocore-exporter &
curl -sf 127.0.0.1:9464/metrics > /tmp/m.txt
promtool check metrics < /tmp/m.txt

promtool check rules exporter/config/technocore-alerts.yml
(cd exporter/config && promtool test rules technocore-alerts.test.yml)
```

`test rules` is the one that matters of the two: `check rules` only says the file parses,
while `test rules` unit-tests all three alerts in both directions — the exactly-85%
capacity boundary that must *not* fire, one exporter of two going down, a scrape slower
than `scrape_timeout`, a healthy pair staying quiet, and nothing scraping the job at all.
It runs from `exporter/config` because the test file names its rule file relatively.

The suite validates the exposition through the client library's own parser on every run, so
`promtool` is a second opinion rather than the only one.
