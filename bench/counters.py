"""What batching the lifetime counters is worth, measured at `store.append` and not at `_bump`.

Run: uv run python bench/counters.py            (add --help for the knobs)

#588: every append bumped one service-wide counter file under a *blocking* exclusive flock,
so writes to unrelated rooms queued behind each other on a file neither of them reads. The
fix makes that flock non-blocking and lets a writer that cannot get it leave its delta in a
process-local bucket for whoever does.

Three things about how this is measured, each of which changes the number:

1. **`store.append`, end to end, not `_bump` alone.** `_bump` in isolation exaggerates: a
   real write also takes the room's own flock, writes a record, fsyncs it and runs the reap
   throttle, and the counter lock is one stage among those. A figure from `_bump` alone is
   the cost of the stage, not the payoff of removing it.

2. **Processes AND threads.** Production is WEB_CONCURRENCY worker processes each serving
   sync handlers out of a threadpool, so the queue on the counter lock is threads-per-worker
   deep times workers. One process with N threads or N processes with one thread each both
   understate it. Workers rendezvous on a `go` file so the rounds actually overlap.

3. **The traffic mix decides the answer.** #601 (closed) measured a sharded counter and found
   traffic concentrated in one room did not move at all, because those writers already
   serialise on that room's own flock — and ~90% of this service's traffic is one room
   (9c7df0e). So the mixes here are the concentrated one, the production-shaped one and a
   spread one, and the concentrated arm is the one that decides whether this is worth
   shipping.

The `blocking` arm is main's own `_bump`, kept verbatim in this file, so "before" stays
reproducible after the source has moved on. The `none` arm removes the counter entirely: it
is not a proposal, it bounds what is left to win. `CHAT_FSYNC=1` throughout — the durable
setting, and the one that puts a real fsync inside the room lock the counter lock competes
with.

Every round ends with a test-only drain in each worker (a `_bump` with no deltas, which is
the ordinary non-blocking flush and not a second implementation of one) and the parent then
checks the persisted total is exact. A batching scheme that is fast because it loses counts
would pass every throughput assertion here and fail that one.

Builds its own store in a tempfile directory — never point it at a real one.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

import store  # noqa: E402

MIXES = ("lobby", "mixed", "spread")
ARMS = ("blocking", "oppo", "batched", "none")
# Not a proposal and not shippable under #588's constraints (it gives up immediate
# persistence on the uncontended path): an arm that flushes only once the bucket holds
# DEFER deltas, to price what deliberate deferral would buy over opportunistic batching.
DEFER = 8
# Flush-on-a-clock, checked inline on the write path (no thread, no timer object): the
# ceiling a deferred trigger can reach, and the freshness it costs to get there.
INTERVALS = (0.05, 1.0)

# The worker: one process, N threads, each writing `writes` messages through the real
# `store.append`. Run as a separate process because the lock under test is a file lock —
# threads in one interpreter contend on it too, but only processes reproduce the shape
# production actually runs.
WORKER = '''
import json, sys, threading, time
sys.path.insert(0, {src!r})
import orjson
import store

root, arm, mix, index, threads, writes, go = (
    store.Path(sys.argv[1]), sys.argv[2], sys.argv[3],
    int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]), store.Path(sys.argv[7]),
)
burst = int(sys.argv[9])


def _bump_blocking(root, **deltas):
    """main@248bcf3's `_bump`, verbatim: one blocking flock around a read-modify-replace."""
    path = root / store.COUNTERS_FILE
    try:
        with store._locked(path):
            current = store.counters(root)
            for key, delta in deltas.items():
                current[key] = current.get(key, 0) + delta
            store._replace(path, orjson.dumps(current))
    except OSError:
        pass


def _bump_oppo(root, **deltas):
    """The opportunistic variant: every bump attempts the flock, batching only what collides.

    Kept here so "before the messages-only rule" stays reproducible after the source moved on.
    """
    from collections import Counter

    batch = Counter()
    with store._PENDING_LOCK:
        store._PENDING.setdefault(root, Counter()).update(deltas)
    try:
        with store._locked(root / store.COUNTERS_FILE, nb=True):
            with store._PENDING_LOCK:
                batch = store._PENDING.pop(root, Counter())
            store._replace(
                root / store.COUNTERS_FILE,
                orjson.dumps(dict(Counter(store.counters(root)) + batch)),
            )
    except OSError:
        with store._PENDING_LOCK:
            store._PENDING.setdefault(root, Counter()).update(batch)


_real_bump = store._bump


def _bump_deferred(root, **deltas):
    """Accumulate, and only attempt the flock once the bucket is worth a write."""
    from collections import Counter

    with store._PENDING_LOCK:
        store._PENDING.setdefault(root, Counter()).update(deltas)
        due = sum(store._PENDING[root].values()) >= {defer}
    if due:
        _real_bump(root)  # no new deltas: this is purely the flush attempt


if arm == "blocking":
    store._bump = _bump_blocking
elif arm == "oppo":
    store._bump = _bump_oppo
elif arm == "none":
    store._bump = lambda *a, **k: None
elif arm == "deferred":
    store._bump = _bump_deferred
elif arm.startswith("every"):
    every, last = float(arm[5:]) / 1000, [0.0]

    def _bump_interval(root, **deltas):
        """Accumulate always; attempt a flush only once `every` seconds have passed.

        The trigger a timer or the reaper would give, without either: nothing fires on its
        own, so a worker that goes quiet holds its last batch until it writes again.
        """
        from collections import Counter

        with store._PENDING_LOCK:
            store._PENDING.setdefault(root, Counter()).update(deltas)
        now = time.monotonic()
        if now - last[0] >= every:
            last[0] = now
            _real_bump(root)

    store._bump = _bump_interval

# One `.counters` replace is one batch reaching disk, so bumps/flushes is the mean batch
# size — the number that says how much of the counter path batching actually removed. A
# name compare on a call that only happens for counters and compaction, never per record.
flushes = [0]
_real_replace = store._replace


def _counting_replace(path, data, fsync=False):
    if path.name == store.COUNTERS_FILE:
        flushes[0] += 1
    return _real_replace(path, data, fsync)


store._replace = _counting_replace

rooms = json.loads(sys.argv[8])
latency = [[] for _ in range(threads)]


def room_for(t, i):
    if mix == "lobby":
        return "lobby"
    if mix == "mixed":
        # Deterministic 90/10 rather than sampled: both arms then write the identical
        # sequence of rooms, so a difference between them cannot be the draw.
        return "lobby" if i % 10 else rooms[(index * threads + t) % len(rooms)]
    return rooms[(index * threads + t) % len(rooms)]


def worker(t):
    mine = latency[t]
    for i in range(writes):
        if burst and i and i % burst == 0:
            time.sleep(0.025)  # idle between spikes: the shape a steady stream never makes
        room = room_for(t, i)
        started = time.perf_counter()
        store.append(root, room, "bench", "m%d-%d-%d" % (index, t, i))
        mine.append(time.perf_counter() - started)


while not go.exists():
    time.sleep(0.002)

started = time.time()
pool = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
for th in pool:
    th.start()
for th in pool:
    th.join()
ended = time.time()

# The test-only drain: flush what this process still holds before it exits, so the parent can
# check the persisted total is exact. A hard exit here is exactly the loss `_bump` documents.
drained = True
for _ in range(1000):
    if root not in store._PENDING:
        break
    _real_bump(root)  # the shipped flush, not whatever arm this run installed over it
else:
    drained = root not in store._PENDING

print(json.dumps({{
    "start": started, "end": ended, "drained": drained, "flushes": flushes[0],
    "latency": [v for mine in latency for v in mine],
}}))
'''


def _percentiles(values: list[float]) -> dict[str, float]:
    """p50/p95/p99 in milliseconds, from every append in the round."""
    ordered = sorted(values)
    out = {
        f"p{p}": ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))] * 1000
        for p in (50, 95, 99)
    }
    return {**out, "max": ordered[-1] * 1000}


def _round(
    root: Path, arm: str, mix: str, procs: int, threads: int, writes: int, fsync: str, burst: int
) -> dict:
    """One round: pre-create every room, then run `procs` workers over it at once."""
    rooms = [f"room{i:02d}" for i in range(procs * threads)]
    seeded = 0
    for room in ["lobby", *rooms]:
        store.append(root, room, "bench", "seed")  # pre-created: no create cost in the timing
        seeded += 1
    go = root / "go"
    script = root / "worker.py"
    script.write_text(WORKER.format(src=SRC, defer=DEFER))
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CHAT_")},
        # 1 puts an fsync inside the room lock; 0 is what a deployment trading that window
        # for headroom runs, and it makes the counter path a *larger* share of a write.
        "CHAT_FSYNC": fsync,
    }
    running = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(root),
                arm,
                mix,
                str(i),
                str(threads),
                str(writes),
                str(go),
                json.dumps(rooms),
                str(burst),
            ],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        for i in range(procs)
    ]
    time.sleep(0.6)  # let every worker reach the rendezvous before any of them starts writing
    go.write_text("")
    reports = []
    for proc in running:
        out, _ = proc.communicate()
        if proc.returncode != 0:
            raise SystemExit(f"worker failed ({proc.returncode}): {out[-2000:]}")
        reports.append(json.loads(out))

    total = procs * threads * writes
    span = max(r["end"] for r in reports) - min(r["start"] for r in reports)
    persisted = store.counters(root)["messages"]
    return {
        "throughput": total / span,
        **_percentiles([v for r in reports for v in r["latency"]]),
        # Exactness is only claimed where a counter is claimed: the `none` arm writes none.
        "exact": None if arm == "none" else persisted == total + seeded,
        "drained": all(r["drained"] for r in reports),
        "persisted": persisted,
        "expected": total + seeded,
        # Bumps per flush: 1.0 means every bump paid the full read-modify-replace and
        # batching removed nothing; higher means that many bumps rode on one write.
        "batch": (total / flushed) if (flushed := sum(r["flushes"] for r in reports)) else None,
    }


def run(
    arms,
    mixes,
    procs: int,
    threads: int,
    writes: int,
    rounds: int,
    raw: bool,
    fsync: str,
    burst: int,
) -> None:
    print(
        f"{procs} processes x {threads} threads x {writes} writes, {rounds} rounds, "
        f"CHAT_FSYNC={fsync}, python {sys.version.split()[0]}"
    )
    for mix in mixes:
        print(f"\n--- {mix}")
        print(
            f"{'arm':<10}{'writes/s':>12}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>9}{'bumps/flush':>13}  counters"
        )
        for arm in arms:
            got = []
            for _ in range(rounds):
                with tempfile.TemporaryDirectory() as tmp:
                    got.append(_round(Path(tmp), arm, mix, procs, threads, writes, fsync, burst))
            median = {
                k: statistics.median([g[k] for g in got])
                for k in ("throughput", "p50", "p95", "p99", "max")
            }
            exact = [g["exact"] for g in got]
            drained = all(g["drained"] for g in got)
            verdict = (
                "n/a (no counter)"
                if exact[0] is None
                else f"exact in {sum(bool(e) for e in exact)}/{rounds}"
                f"{'' if drained else ', UNDRAINED'}"
            )
            sizes = [g["batch"] for g in got if g["batch"] is not None]
            batch = f"{statistics.median(sizes):.2f}" if sizes else "-"
            print(
                f"{arm:<10}{median['throughput']:>12,.0f}{median['p50']:>10.2f}"
                f"{median['p95']:>10.2f}{median['p99']:>10.2f}{median['max']:>9.1f}{batch:>13}  {verdict}"
            )
            if raw:  # every round, so a small median difference can be read against the spread
                print(f"{'':10}rounds: " + "  ".join(f"{g['throughput']:,.0f}" for g in got))
                print(f"{'':10}p99s:   " + "  ".join(f"{g['p99']:.2f}" for g in got))
            if not all(e in (None, True) for e in exact):
                off = [(g["persisted"], g["expected"]) for g in got if g["exact"] is False]
                print(f"{'':10}counters WRONG: persisted vs expected {off}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--procs", type=int, default=3)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--writes", type=int, default=500, help="per thread")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--mixes", default=",".join(MIXES))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--raw", action="store_true", help="print every round, not just the median")
    ap.add_argument("--fsync", default="1", choices=("0", "1"), help="CHAT_FSYNC for the run")
    ap.add_argument("--burst", type=int, default=0, help="idle 25ms every N writes (0: steady)")
    args = ap.parse_args()
    run(
        [a for a in args.arms.split(",") if a],
        [m for m in args.mixes.split(",") if m],
        args.procs,
        args.threads,
        args.writes,
        args.rounds,
        args.raw,
        args.fsync,
        args.burst,
    )


if __name__ == "__main__":
    main()
