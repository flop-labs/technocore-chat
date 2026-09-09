# Bounded multi-room reads: current-main results

## Scope

This is a client-side measurement for [issue #767](https://github.com/flop-labs/technocore-chat/issues/767), not a batch-endpoint implementation. It compares the existing `GET /r/{room}` API used sequentially with the same API used through a persistent connection pool and a bounded thread pool. It does not change the server or reduce the number of requests.

Upstream commit: `20a4457b89ba11254f4aa48217b066884a148d98` (main retrieved 2026-09-08T15:39:08.580Z).

## Linux validation and method

- Runner: WSL2 Linux 6.18.33.2, Alpine 3.21.7, CPython 3.12.13, uv 0.12.3.
- Dependencies: the upstream `uv.lock`, installed with `uv sync --frozen`; no compatibility shim.
- Server: one worker, bound to `127.0.0.1`; production room-read handler and store code.
- Fixture: 16 rooms, 20 messages per room, 128 ASCII text bytes per message; 61,296 stored JSONL bytes.
- Read: `since=0`, `limit=20` for each room; 320 messages returned per sample.
- Artificial delay: 25 ms once before every `GET /r/*` enters the ASGI app.
- Connection reuse: enabled in both arms. Parallel limits: 2, 4, and 8.
- Warm-up: 2 samples per arm. Measurement: 20 samples per arm and concurrency.
- Order: sequential/parallel order alternated each repetition.
- Timeout: 5 seconds per room. No automatic retries.
- Correctness: every measured response set had the same digest, 71,440 response-body bytes, zero errors, and zero detected cursor gaps.

## Linux results

| Method | Bound | Median | p95 | Std dev | Median speedup | Requests | Body bytes | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Sequential | 1 | 463.21 ms | 467.02 ms | 2.06 ms | baseline | 16 | 71,440 | 0 |
| Bounded parallel | 2 | 242.03 ms | 251.11 ms | 5.32 ms | 1.91x | 16 | 71,440 | 0 |
| Sequential | 1 | 465.49 ms | 472.66 ms | 3.27 ms | baseline | 16 | 71,440 | 0 |
| Bounded parallel | 4 | 133.00 ms | 148.90 ms | 7.77 ms | 3.50x | 16 | 71,440 | 0 |
| Sequential | 1 | 466.52 ms | 469.60 ms | 2.14 ms | baseline | 16 | 71,440 | 0 |
| Bounded parallel | 8 | 88.48 ms | 94.42 ms | 5.63 ms | 5.27x | 16 | 71,440 | 0 |

The measured client CPU medians were 31-51 ms, but this does not include server CPU. Client RSS,
server CPU, server memory, worker occupancy, header/framing bytes, TLS/proxy effects, packet loss,
and third-party reproduction are `NOT_MEASURED`.

## Separate Windows result

A native Windows exploratory run used a read-only `fcntl` compatibility shim and disabled ASGI
lifespan because current main uses POSIX `fcntl`/`fchmod`. Its bounded-parallel median speedups were
1.63x (2), 2.82x (4), and 4.58x (8). Those results are not part of the Linux validation and 4.58x
is not a general performance guarantee. The public reproduction recipe uses no shim.

## Interpretation

Bounded concurrency hid the controlled per-request delay, but it did not reduce request count or transferred response bytes. Eight workers was the fastest latency arm here, but server resource usage was not measured, so this result is not evidence that eight is a safe universal default. Four is a reasonable example bound, not a server recommendation; clients should lower it when they observe `429`, timeouts, or resource pressure.

Each result remains an independent room snapshot. The client retains the room's `generation`, cursor, and sequence bounds, flags a possible retention/limit gap, isolates room errors, records `429` without retry, and closes the pool/executor at exit. It never claims to recover data outside the retained ring.

## Raw data

- `bench/results/multi-room-reads-linux-20a4457b-c2.jsonl`
- `bench/results/multi-room-reads-linux-20a4457b-c2.summary.json`
- `bench/results/multi-room-reads-linux-20a4457b.jsonl` (bound 4)
- `bench/results/multi-room-reads-linux-20a4457b.summary.json` (bound 4)
- `bench/results/multi-room-reads-linux-20a4457b-c8.jsonl`
- `bench/results/multi-room-reads-linux-20a4457b-c8.summary.json`

The batch arm requested by #767 was not implemented or measured, so this work does not claim to close the issue.
