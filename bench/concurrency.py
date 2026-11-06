"""What the per-IP token bucket grants when callers from one IP arrive at once.

Every other measurement in this repo is serial: `tests/capacity_bench.py` drives the
store and the HTTP layer one request at a time, so it can never see a lost update. But the
limiter's whole job is to bound *concurrent* callers, and `limit.take()` runs in Starlette's
threadpool for every sync write handler — so two requests from one IP that read the same
bucket balance before either writes it back would both spend the same token.

`_buckets_lock` (#163) is what stops that, and its regression tests prove it by forcing one
interleaving. This is the other half: a natural burst, how far it overshoots the cap, as a
function of contention and of the scheduling quantum. The answer it should give is zero at
every row, and a nonzero row means the lock has stopped covering the window.

    # primitive only — calls limit.take() from threads, no server, no store:
    python bench/concurrency.py
    python bench/concurrency.py --cap 100 --threads 1,2,8,64,256,1000 --rounds 40

    # below the width of the critical section, where only the lock can be holding it:
    python bench/concurrency.py --sweep-switch-interval

    # with a server, to also measure it through the real ASGI threadpool:
    CHAT_ROOT="$(mktemp -d)" CHAT_RATE_WRITE=100 CHAT_RATE_READ=100000 \
        CHAT_RATE_ROOMS_PER_DAY=100000 \
        uv run uvicorn --app-dir src app:app --port 8099 --workers 1 &
    python bench/concurrency.py --port 8099 --cap 100

What "overshoot" means here: from a bucket holding `cap` tokens, firing T concurrent grants
should grant exactly min(T, cap) — no more — under a correct read-modify-write. Anything above
that is a token handed out twice. The figure is reported as grants, the min(T, cap) ceiling,
and the excess; the excess is the lost-update count.

WHAT IT MEASURED
----------------
The same bench against the commit before the lock and a commit with it. Clock frozen, cap 100,
1000 threads, 20 rounds, `--sweep-switch-interval` on CPython 3.12.14:

                        6571499, before #163     73a7324, with it
    switchinterval      excess max   leaking     excess max   leaking
    0.005 (default)              0      0/20              0      0/20
    0.0001                       0      0/20              0      0/20
    1e-06                       78     20/20              0      0/20
    1e-09                       85     20/20              0      0/20

The top two rows are why a zero at the default quantum proves nothing: the unlocked code reads
zero there too. Its read-modify-write contains no blocking call, so a thread that acquires the
GIL ran it to completion inside its quantum, and the crossover puts that section near a
microsecond. The lock is what makes the bottom two rows zero.

Free-threaded CPython 3.13.15 (`gil=DISABLED`), same bench, cap 100, 20 rounds:

    threads   6571499: grants min  max  excess max     73a7324: grants min  max  excess max
         32                    32   32           0                      32   32           0
        128                   128  128          28                     100  100           0
        256                   256  256         156                     100  100           0
        512                   512  512         412                     100  100           0
       1000                  1000 1000         900                     100  100           0

Above the cap, before the lock, min and max both equal the thread count in every round: the
bucket was not being raced so much as ignored. PEP 779 made free-threading officially supported
in 3.14. Those two runs had orjson and nacl stubbed on PYTHONPATH, because limit.py imports
store and store imports both, and take() reaches neither:

    PYTHONPATH=<stubs> uv run --no-project --python 3.13t --with starlette \
        python bench/concurrency.py --cap 100 --threads 32,128,256,512,1000 --rounds 20

Method notes, so a single run is not read as a law:
  - Best-of is wrong for a race. A lock either holds or it does not, so the interesting
    number is the WORST round (the most tokens lost) and how often a round loses any at all,
    not the minimum. Both are reported.
  - The bucket refills while a burst runs, and those are real tokens, not raced ones. At a
    switch interval of 1e-06 a burst takes about two seconds, which at per_min == 100 earns
    three of them. So the primitive bench freezes the clock take() reads, and through HTTP,
    where it cannot, what the burst earned joins the ceiling.
  - The HTTP run refuses to start unless `/config` reports `--cap` as the server's rate_write.
    An excess computed against a ceiling the server is not using is a labelling error.
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
import json
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import limit  # noqa: E402

# take() earns tokens from `limit.time.monotonic()`, the only thing limit.py reads off that
# module. A burst that takes two seconds at per_min == cap earns real tokens, and those would
# read as a race. With this in place they cannot: every token granted past the bucket's
# contents is a lost update.
_FROZEN_CLOCK = types.SimpleNamespace(monotonic=lambda: 1000.0)


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

    with mock.patch.object(limit, "time", _FROZEN_CLOCK), ThreadPoolExecutor(threads) as pool:
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
        "  and 0 at every row is what a locked take() gives. The refill clock is frozen, so a\n"
        "  slow burst earns nothing and an excess of 1+ is the race, not refill."
    )


def _hit(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 — a dropped connection under flood is data, not a crash
        return 0


def _require_served_cap(port: int, cap: int) -> None:
    """Refuse the HTTP run unless the server itself says its write bucket holds `cap`.

    `--cap` is only what this process believes, and excess is computed against it. A server at
    100 told `--cap 50` grants 80 writes and prints an excess of 30 with nothing raced, and the
    opposite mismatch hides a real overshoot. `/config` publishes the effective limit, so the
    belief can be checked rather than trusted, and it is checked before any bench runs.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=15) as r:
            served = json.load(r)["settings"]["rate_write"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(
            f"refusing: could not read rate_write from /config on {port}: {e!r}"
        ) from e
    if served != cap:
        raise SystemExit(
            f"refusing: --cap {cap} is not this server's write cap, /config says rate_write="
            f"{served}. An excess computed against the wrong ceiling is a labelling error."
        )


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
    # The server's clock cannot be frozen from here, so what the burst earned joins the ceiling.
    earned = int(dur / 1000 * cap / 60)
    ceiling = min(threads, cap + earned)
    print(f"  burst finished in {dur:.0f} ms")
    print(f"  {'2xx (granted)':<16} {ok}")
    print(f"  {'429 (limited)':<16} {limited}")
    if other:
        print(
            f"  {'other/failed':<16} {other}  (codes: "
            f"{sorted({c for c in codes if c not in (200, 201, 429)})})"
        )
    print(f"  {'earned in burst':<16} {earned}")
    print(f"  {'ceiling':<16} {ceiling}   <- min(N, cap + earned)")
    print(f"  {'excess':<16} {ok - ceiling}   <- tokens the limiter granted beyond that")


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

    This is here because a zero at the default quantum proves little: take()'s read-modify-write
    makes no blocking call, so a thread that gets the GIL finishes it inside its quantum with or
    without a lock. Shrinking the quantum below the window removes that accident, on the same
    interpreter, with nothing patched. Zeros at every row are the lock doing it, and a leak at
    any row means the lock has stopped covering the window.
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
        "  The clock is frozen, so a leak at any row is a lost update. Zeros all the way down\n"
        "  are what the lock gives; the module docstring has this same sweep from before it."
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
        help="bucket capacity and per_min for the primitive bench, and the rate_write the "
        "server under --port has to report on /config, or the run is refused",
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
        "than take()'s critical section, so it finds nothing even without a lock",
    )
    parser.add_argument(
        "--sweep-switch-interval",
        action="store_true",
        help="walk the scheduling quantum instead of the thread count, to check the lock "
        "below the width of its window (see the module docstring). Needs a --threads level above "
        "--cap, since below the cap there is no overshoot to find",
    )
    args = parser.parse_args()
    _validate(parser, args)

    if args.switch_interval is not None:
        sys.setswitchinterval(args.switch_interval)
    if args.port:
        _require_served_cap(args.port, args.cap)
    print(f"{_interpreter()}\nlimit.MAX_BUCKETS={limit.MAX_BUCKETS}  cap(under test)={args.cap}")
    if args.sweep_switch_interval:
        sweep_switch_interval(args.cap, max(t for t in args.threads if t > args.cap), args.rounds)
        return
    primitive_bench(args.cap, args.threads, args.rounds)
    if args.port:
        http_bench(args.port, args.cap, max(args.threads))


if __name__ == "__main__":
    main()
