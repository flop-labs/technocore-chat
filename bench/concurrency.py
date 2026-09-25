"""What the per-IP token bucket grants when callers from one IP arrive at once.

Every other measurement in this repo is serial: `tests/capacity_bench.py` drives the
store and the HTTP layer one request at a time, so it can never see a lost update. But the
limiter's whole job is to bound *concurrent* callers, and `limit.take()` runs in Starlette's
threadpool for every sync write handler — so two requests from one IP can read the same
bucket balance before either writes it back, and both spend the same token. This measures
how many tokens that costs, as a function of how many callers contend.

It is the number the open lock proposals argue from without one: #163 and #420 both show the
race exists with a forced interleaving and close it with a lock. This says how far a *natural*
burst overshoots the cap, and how the overshoot scales with contention — the thing that decides
whether a lock is worth its core lines. `uv run sz.py --caps` prices that side; src/limit.py
has no headroom under its cap, so the ceiling has to move for any lock to land.

    # primitive only — calls limit.take() from threads, no server, no store:
    python bench/concurrency.py
    python bench/concurrency.py --cap 100 --threads 1,2,8,64,256,1000 --rounds 40

    # why the default run finds nothing (see below) — walk the GIL switch interval:
    python bench/concurrency.py --sweep-switch-interval

    # with a server, to also measure it through the real ASGI threadpool:
    CHAT_ROOT="$(mktemp -d)" CHAT_RATE_WRITE=100 CHAT_RATE_READ=100000 \
        CHAT_RATE_ROOMS_PER_DAY=100000 \
        uv run uvicorn --app-dir src app:app --port 8099 --workers 1 &
    python bench/concurrency.py --port 8099 --cap 100

What "overshoot" means here: from a bucket holding `cap` tokens, firing T concurrent grants
should grant exactly min(T, cap) — no more — under a correct read-modify-write. Anything above
that is a token handed out twice. The figure is reported as grants, the min(T, cap) ceiling,
and the excess; the excess is the lost-update count, and zero is the answer a locked take()
should give at every T.

WHAT IT MEASURED, and why the answer is not the expected one
------------------------------------------------------------
On CPython 3.12 (the version CI pins) the answer is **zero excess at every contention level**,
1 through 1000 threads, 30 rounds each — 240 bursts, not one lost token. Taken alone that
reads as "take() is thread-safe", and it is the wrong conclusion.

The race is real; the default run cannot reach it. The read-modify-write in take() —
`_buckets.get(...)`, arithmetic, `_buckets[...] = ...` — contains no blocking call, so a
thread that acquires the GIL runs it to completion inside its scheduling quantum. Lower the
quantum below the width of that window and the same code, unchanged, loses updates in every
round (`--sweep-switch-interval --rounds 20`, cap 100, 1000 threads):

    switchinterval=0.005    excess max=   0   leaking  0/20     <- the default
    switchinterval=0.0001   excess max=   0   leaking  0/20
    switchinterval=1e-06    excess max=  58   leaking 20/20
    switchinterval=1e-09    excess max=  60   leaking 20/20

The crossover between 1e-4 and 1e-6 puts the critical section near a microsecond, three orders
of magnitude under the 5 ms default. So the limiter's correctness here is a property of the
scheduler's timing, not of the code: nothing in take() establishes it, and nothing preserves
it. Widen the window — an added dict lookup, a log line, a debug hook — and it comes back.

Remove the coincidence entirely and the bug is not marginal. Free-threaded CPython 3.13.15
(`gil=DISABLED`), same burst, cap 100 — the bench runs there as-is:

    uv run --no-project --python 3.13t --with starlette \
        python bench/concurrency.py --cap 100 --threads 32,128,256,512,1000 --rounds 20

    threads  ceiling  grants: min   max  excess max  rounds leaking
         32       32           32    32           0    0/20  (   0%)
        128      100          128   128          28   20/20  ( 100%)
        256      100          256   256         156   20/20  ( 100%)
        512      100          512   512         412   20/20  ( 100%)
       1000      100         1000  1000         900   20/20  ( 100%)

Read the min and max columns before the excess: above the cap they equal the thread count, in
every round, at every level. Not "sometimes loses a token" — every caller was granted, with no
variance, so the bucket is not being raced so much as ignored. The overshoot tracks demand,
which means the limiter fails hardest under exactly the flood it exists to stop. Below the cap
the excess is 0 because every caller is entitled to a token there; the race only surfaces as
overshoot once demand exceeds the cap, which is the only regime that matters. PEP 779 made
free-threading officially supported in 3.14, so this is the next interpreter, not a
hypothetical one.

That is the case for the lock, and it is a different case than "the race exists": the fix buys
nothing measurable on today's CI interpreter, and it is unconditional the moment anyone runs
this free-threaded. Whoever weighs its core lines should weigh them against that, not against
the zeros in the default run.

One failure mode the excess column does not cover, because it is not an overcount: #378 reports
a KeyError in the same four lines — `__setitem__` leaves an existing key where it is, so another
thread's `popitem(last=False)` can evict it before `move_to_end` runs, which needs the table
already over `max_buckets`. The pop-then-insert fix proposed there closes that without a lock,
and closes none of this: the read-modify-write on the *balance* stays unserialised either way,
and the balance is what the excess column counts. Two failures in one critical section, and the
cheap fix addresses one of them.

Method notes, so a single run is not read as a law:
  - Best-of is wrong for a race. A lock either holds or it does not, so the interesting
    number is the WORST round (the most tokens lost) and how often a round loses any at all,
    not the minimum. Both are reported.
  - The bucket refills while the burst runs, which adds real tokens, not raced ones. At
    per_min == cap that is cap/60 tokens per second; a 1000-thread burst that finishes in
    tens of ms adds well under one, so an integer excess of 1+ is the race, not the clock.
    Raising --cap without shortening the burst reintroduces this — the docstring figure is
    fabricated if the two are not reported together.
  - A zero row is evidence about this interpreter and this switch interval, and about nothing
    else. Both are printed in the header for that reason: a figure quoted without them says
    less than it appears to.
  - Absolute timings move ~20% with the host between runs with no code change (see
    capacity_bench.py). The *excess token count* does not: it is an integer property of the
    interleaving, so compare that across a fix, not the milliseconds.

Establish a baseline on the machine you care about before reading any single figure as a
regression, and pin every number to the caps it was taken under: a measurement re-labelled
with today's constants is a fabricated measurement.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import limit  # noqa: E402


def _request(ip: str):
    """A minimal ASGI scope is enough for client_ip(): it reads request.client.host and,
    only when a proxy header is configured, request.headers. One IP, no proxy headers, so
    every caller in a run shares one bucket — which is the contention this measures."""
    from starlette.requests import Request

    return Request({"type": "http", "headers": [], "client": (ip, 0), "method": "GET", "path": "/"})


def _one_burst(cap: int, threads: int) -> int:
    """Fire `threads` concurrent take() calls from one IP against a full bucket, all lined
    up on a barrier so they enter the read-modify-write together. Returns how many were
    granted (wait == 0.0). A correct limiter grants min(threads, cap)."""
    limit._buckets.clear()
    req = _request("10.0.0.1")
    barrier = threading.Barrier(threads)
    granted = 0
    lock = threading.Lock()  # guards the tally only, never the code under test

    def worker() -> None:
        nonlocal granted
        barrier.wait()
        # per_min == burst == cap: a full bucket is exactly `cap` tokens (see take()).
        _left, wait = limit.take(req, "write", cap, burst=cap)
        if wait == 0.0:
            with lock:
                granted += 1

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for _ in range(threads):
            pool.submit(worker)
    return granted


def primitive_bench(cap: int, thread_counts: list[int], rounds: int) -> None:
    print(
        f"\nprimitive — limit.take() from N threads, one IP, bucket of {cap} tokens "
        f"(per_min={cap}), {rounds} rounds each"
    )
    print(
        f"  {'threads':>7}  {'ceiling':>7}  {'grants: min':>11}  {'max':>4}  "
        f"{'excess max':>10}  {'rounds leaking':>14}"
    )
    for t in thread_counts:
        ceiling = min(t, cap)
        grants = [_one_burst(cap, t) for _ in range(rounds)]
        excess = [g - ceiling for g in grants]
        leaking = sum(1 for e in excess if e > 0)
        print(
            f"  {t:>7}  {ceiling:>7}  {min(grants):>11}  {max(grants):>4}  "
            f"{max(excess):>10}  {leaking:>3}/{rounds:<3} ({100 * leaking / rounds:4.0f}%)"
        )
    print(
        "  excess is tokens granted beyond the bucket's contents — a lost update, an integer,\n"
        "  and 0 at every row is what a locked take() gives. The clock adds < 1 token over a\n"
        "  sub-second burst at per_min == cap, so an excess of 1+ is the race, not refill."
    )


def _hit(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 — a dropped connection under flood is data, not a crash
        return 0


def http_bench(port: int, cap: int, threads: int) -> None:
    """The same contention through the real ASGI threadpool. Each request writes a UNIQUE
    line, so the cross-sender duplicate filter keys every one of them to its own slot and
    refuses none — every request genuinely reaches take(). That is a load-bearing assumption
    rather than a hope: a duplicate refusal is a 422, which is neither a grant nor a 429, so
    it would land in the other/failed row with its code printed instead of quietly deflating
    the grant count. Writes go to an existing room so the separate `create` budget is not in
    play. Grants are 2xx; a spent budget is 429."""
    base = f"http://127.0.0.1:{port}"
    room = "concurrency-bench"
    # Bring the room into existence first (its own request, before the burst), then let the
    # write bucket refill to full — cap/ (per_min=cap) = 60s to be safe about a clean start.
    print(f"\nhttp — {threads} concurrent writes to /r/{room} on {base}, write cap {cap}")
    if _hit(f"{base}/r/{room}/say/warmup/seed") not in (200, 201):
        print(
            "  could not create the room — is the server up and the write budget high enough"
            " for one seed write? aborting http bench."
        )
        return
    wait_s = min(65.0, cap * 60.0 / cap + 5.0)
    print(f"  waiting {wait_s:.0f}s for the write bucket to refill to {cap} before the burst …")
    time.sleep(wait_s)

    urls = [
        f"{base}/r/{room}/say/n{i}/msg-{i}-{int(time.perf_counter() * 1e6)}" for i in range(threads)
    ]
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=threads) as pool:
        codes = list(pool.map(_hit, urls))
    dur = (time.perf_counter() - start) * 1000

    ok = sum(1 for c in codes if c in (200, 201))
    limited = sum(1 for c in codes if c == 429)
    other = threads - ok - limited
    ceiling = min(threads, cap)
    print(f"  burst finished in {dur:.0f} ms")
    print(f"  {'2xx (granted)':<16} {ok}")
    print(f"  {'429 (limited)':<16} {limited}")
    if other:
        print(
            f"  {'other/failed':<16} {other}  (codes: "
            f"{sorted({c for c in codes if c not in (200, 201, 429)})})"
        )
    print(f"  {'ceiling min(N,cap)':<16} {ceiling}")
    print(f"  {'excess':<16} {ok - ceiling}   <- tokens the limiter granted beyond the cap")
    if dur > 2000:
        print(
            "  NOTE: burst took over 2s; at per_min == cap the bucket refilled meaningfully "
            "during it,\n        so some of the excess is the clock. Shorten it or lower --cap "
            "and re-read."
        )


def _thread_list(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def _interpreter() -> str:
    """The two facts that decide whether a zero row means anything.

    A run of zeros is evidence about one interpreter at one scheduling quantum, so both are
    printed beside the numbers rather than left for the reader to assume. `_is_gil_enabled` is
    3.13+; on an older build its absence is itself the answer.
    """
    probe = getattr(sys, "_is_gil_enabled", None)
    gil = "enabled" if probe is None else ("enabled" if probe() else "DISABLED (free-threaded)")
    return (
        f"python {sys.version.split()[0]}  gil={gil}  switchinterval={sys.getswitchinterval():g}s"
    )


def sweep_switch_interval(cap: int, threads: int, rounds: int) -> None:
    """Walk the GIL's scheduling quantum past the width of take()'s critical section.

    This is here because the default run reports zero overshoot at every contention level, and
    that number is worth less than it looks: take()'s read-modify-write holds no lock and makes
    no blocking call, so a thread that gets the GIL finishes it inside its quantum. Nothing in
    the code establishes that — it is the scheduler's timing doing it. Shrinking the quantum
    below the window is the cheapest way to show the same lines losing updates, on the same
    interpreter, with nothing patched.

    Read the crossover, not the individual rows: where the zeros stop is roughly how wide the
    unprotected window is, and how much headroom the 5 ms default is currently providing.
    """
    original = sys.getswitchinterval()
    print(
        f"\nswitch-interval sweep — {threads} threads, bucket of {cap}, {rounds} rounds each.\n"
        f"  The code under test does not change between rows; only the scheduler does."
    )
    print(f"  {'switchinterval':>16}  {'excess max':>10}  {'rounds leaking':>14}")
    try:
        for interval in (5e-3, 1e-4, 1e-6, 1e-9):
            sys.setswitchinterval(interval)
            ceiling = min(threads, cap)
            excess = [_one_burst(cap, threads) - ceiling for _ in range(rounds)]
            leaking = sum(1 for e in excess if e > 0)
            print(
                f"  {interval:>16g}  {max(excess):>10}  "
                f"{leaking:>3}/{rounds:<3} ({100 * leaking / rounds:4.0f}%)"
            )
    finally:
        sys.setswitchinterval(original)  # a bench that leaves this lowered poisons later rows
    print(
        "  Zeros at the top and leaks at the bottom mean the limiter is correct here by\n"
        "  scheduling accident, not by construction. A free-threaded build removes the accident\n"
        "  outright — see the module docstring for those numbers."
    )


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse invocations that cannot produce a measurement, before any thread starts.

    Every case below otherwise surfaces as a traceback from inside a bench — `max()` on an empty
    sequence, or a division by a zero round count — which reads as a broken benchmark rather than
    a bad command line. An argparse error says which it was, with a usage line and exit 2.
    """
    if args.cap < 1:
        parser.error("--cap must be at least 1: a bucket of 0 tokens grants nothing to count")
    if args.rounds < 1:
        parser.error("--rounds must be at least 1: 0 rounds leaves no bursts to summarise")
    if not args.threads:
        parser.error("--threads needs at least one contention level")
    if bad := [t for t in args.threads if t < 1]:
        parser.error(f"--threads values must be at least 1, got {bad}")
    if args.switch_interval is not None and args.switch_interval <= 0:
        parser.error(
            "--switch-interval must be greater than 0; the interpreter rejects 0 and below"
        )
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error(f"--port must be in 1..65535, got {args.port}")
    if args.sweep_switch_interval and not any(t > args.cap for t in args.threads):
        # Not merely unmeasurable — actively misleading. Below the cap every caller is entitled
        # to a token, so excess is 0 by construction at every interval. The sweep would print a
        # column of zeros that looks like evidence the limiter is thread-safe, which is the exact
        # misreading this file exists to prevent. Refuse rather than produce it.
        parser.error(
            f"--sweep-switch-interval needs a --threads level above --cap ({args.cap}); got "
            f"{args.threads}. Below the cap excess is 0 by construction, so the sweep would "
            f"report zeros at every interval and measure nothing."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=100,
        help="bucket capacity and per_min for the primitive bench, and the "
        "CHAT_RATE_WRITE the server under --port is expected to enforce",
    )
    parser.add_argument(
        "--threads",
        type=_thread_list,
        default=[1, 2, 4, 8, 32, 128, 512, 1000],
        help="comma-separated contention levels for the primitive bench",
    )
    parser.add_argument("--rounds", type=int, default=30, help="bursts per contention level")
    parser.add_argument("--port", type=int, help="also measure a server already on this port")
    parser.add_argument(
        "--switch-interval",
        type=float,
        help="set sys.setswitchinterval() before measuring. The default 5ms is ~1000x wider "
        "than take()'s unprotected window, which is why the default run finds nothing",
    )
    parser.add_argument(
        "--sweep-switch-interval",
        action="store_true",
        help="walk the scheduling quantum instead of the thread count, to locate the width of "
        "the unprotected window (see the module docstring). Needs a --threads level above "
        "--cap, since below the cap there is no overshoot to find",
    )
    args = parser.parse_args()
    _validate(parser, args)

    if args.switch_interval is not None:
        sys.setswitchinterval(args.switch_interval)
    print(f"{_interpreter()}\nlimit.MAX_BUCKETS={limit.MAX_BUCKETS}  cap(under test)={args.cap}")
    if args.sweep_switch_interval:
        sweep_switch_interval(args.cap, max(t for t in args.threads if t > args.cap), args.rounds)
        return
    primitive_bench(args.cap, args.threads, args.rounds)
    if args.port:
        http_bench(args.port, args.cap, max(args.threads))


if __name__ == "__main__":
    main()
