"""Measure what the capacity walks and the hot read cost at a FULL store.

Not a pytest module (the filename keeps it out of collection): it builds a store at the
configured caps, which takes tens of seconds and hundreds of megabytes of inodes, and it
reports numbers rather than asserting them. The assertions that belong in CI are in
test_app.py — this is the thing you run when you change a cap, a walk, or a cache, because
every one of those is O(store) and none of them shows up on a small store.

    python tests/capacity_bench.py                 # store-level only
    python tests/capacity_bench.py --scale 0.1     # a tenth of the caps, ~10x quicker

    # with a server, to also measure the HTTP layer:
    CHAT_ROOT=/tmp/bench-store CHAT_RATE_READ=5000 CHAT_RATE_WRITE=5000 \\
        uv run uvicorn app:app --app-dir src --port 8099
    python tests/capacity_bench.py --keep /tmp/bench-store --port 8099

Measured 2026-08-19 on the session container (tmpfs), MAX_ROOMS=5120,
MAX_NOTES_TOTAL=40960 — expected shape. Left at the caps it was taken under, because a
measurement re-labelled with today's constants is a fabricated measurement. Three things
have moved since. The global note check stopped walking (`.notes-count`), so the note
create figure below is an upper bound rather than the shape. MAX_NOTES_TOTAL is now
32 * MAX_ROOMS = 163,840. And note_stats no longer walks at all: it was 124 ms at 40960
and 480 ms at 163840 when re-measured on tmpfs, and is ~0.1 ms now, so the line below is
the cost that change removed rather than a cost anyone still pays:

  store, per NEW room/note (the create path, serialised behind the create gate):
    _check_room_capacity                 ~16 ms   count + byte budget, one scandir pass.
                                                  Still O(rooms), deliberately: the byte
                                                  total has to be exact, and the scan that
                                                  gets it returns the count anyway
    _check_note_capacity                  ~0 ms   was ~25 ms. The global cap reads
                                                  .notes-count instead of walking every
                                                  namespace; only the per-namespace cap
                                                  still scans, and that is O(one caller's
                                                  own namespace). Measured flat at 0.3 ms
                                                  across 4k, 14k and 28k notes — if this
                                                  starts tracking store size again, the
                                                  count file is being rebuilt every call
  store, per reap pass (write path, at most once per REAP_EVERY):
    _reap                               ~630 ms   dominated by one stat() per file, which
                                                  no walk avoids; the walk is the small
                                                  half. Includes the usage walk that
                                                  refreshes .usage for the adaptive ring
  store, per /rooms request if uncached:
    room_stats(limit=200)                ~39 ms
    note_stats                          ~135 ms   40960 stat() calls for one summary line
  walk primitives, glob vs scandir, back to back on one store:
    rooms  glob  14 ms -> _walk  9 ms
    notes  glob 112 ms -> _walk 72 ms
    (_scan is faster still where only a count is needed: it allocates no Path per entry)

  Absolute numbers move with the host, and by more than you would guess: the same container
  measured every one of these ~20% faster a few hours earlier, `glob rooms` included, with
  no code between the two runs. Compare the *ratios* — and re-run this to establish a
  baseline on the machine you actually care about before reading any single figure as a
  regression.

  http (every figure here is measured through `curl`, so ~6 ms of process spawn is the
  floor — the /healthz rows below measure that floor as much as anything):
    GET /rooms cold                     ~171 ms
    GET /rooms cached                     ~7 ms   i.e. at the floor. ROOMS_CACHE_SECONDS.
                                                  A message no longer ends the window; a
                                                  create, reap or topic still does
    event loop unserved during one write:
      GET  /r/<room>/say/...             ~25 ms   sync endpoint: Starlette threadpools it
      POST /r/<room>                     ~11 ms   was ~385 ms before it was threadpooled

Measured 2026-08-25 for 0.9.2, on the same container (tmpfs), against the 0.9.1 code in the
same alternating run so host drift cancels. This one varies ONE namespace rather than the
store, which is the axis the block above never moved — its "flat at 0.3 ms across 4k, 14k
and 28k notes" spread those notes over many namespaces, so the per-namespace scan it was
timing never grew. Concentrate them, as the `did` namespace does in production, and the
shape it was reading as flat is a line:

  store, per NEW note into ONE namespace (with a sidecar lock per note, as production has):
    notes in namespace     0.9.1      0.9.2
             1,000       3.70 ms    3.31 ms    1.1x
             4,000       7.26 ms    1.25 ms    5.8x
            10,240      14.58 ms    1.21 ms   12.1x   production's `did` at the time
            20,480      26.67 ms    1.32 ms   20.2x   4 * MAX_ROOMS, a widened namespace
    0.9.1 is linear in the namespace, 0.9.2 is flat: the count comes from that namespace's
    own count file, so the create path reads two numbers and walks nothing. What is left is
    ~14 open()s and 4 atomic replaces per create, none of which scale with anything
  store, per /rooms request if uncached, 1,200 rooms:
    room_stats(limit=50)  6.61 ms -> 5.14 ms   memoizing `_listable`; the walk is now within
                                               a sixth of its floor of one stat() per room

Measured 2026-08-25 for 0.9.3, same container (tmpfs). The signed-write section builds its
own room and ignores --scale, so these do not move with the caps:

  per _last_nonce call, 8,255 records of 196 B, of which READ_BUDGET covers ~5,300:
                                  scan-only   parse every   bytes reject
    DID absent from the window       0.8 ms        3.9 ms         2.2 ms   1.8x
    DID posted 3 records ago             n/a       ~0 ms          ~0 ms
    DID quoted in every record       0.8 ms        3.9 ms         5.9 ms   0.6x
    scan-only is reverse_lines with no parse: the floor. The absent-DID case went from
    over 5x it to under 3x. The last row is the adversarial shape — every line a false
    positive, so the filter is pure overhead on top of the parse.

  Under cProfile the absent-DID case (shares, not ms — cProfile inflates totals):
    parse every    26% _parse · 23% the scan loop · 21% orjson.loads · 11% dict.get
    bytes reject   63% the scan loop · 17% reverse_lines · 14% bytes.split · 3% read
  The parse leaves the profile; what remains is reading the window.

Measured 2026-08-25 for the /rooms stamp change, same container (tmpfs), 10,240 rooms, at
production's mix — 24 messages/second and 2.85 /rooms/second — over a 20s window, before and
after in one session so host drift cancels. `newfstatat` is counted with `strace -f -c` on
the worker (no bpftrace in this container; it counts the same syscall), and a "walk" is a
/rooms response over 50 ms, which an uncached one is by an order of magnitude:

                                     before     after
    /rooms requests served               58        58
    of those, walks                      50         6    one per ROOMS_CACHE_SECONDS
                                                         rather than one per request
    median /rooms latency           52.6 ms    2.5 ms
    newfstatat per /rooms request    12,389     1,807    under strace, which serves fewer
                                                         requests but stats the same per walk

  The after figures are set by the clock and not by the traffic: the walk rate is capped at
  1/ROOMS_CACHE_SECONDS whatever the request rate, so the ratio keeps falling the harder
  /rooms is polled, and what is left of it is the write path (~58 stats per append) rather
  than the walk. Before, `messages` was in the cache stamp — at 24 messages/second it turned
  over ~72 times per 3s window, so the cache was correct, never hit, and every request walked
  every room.

  `rooms_cache_bench` below is the same measurement without a server: it counts the walk at
  the call rather than inferring it from a syscall total or a latency, and runs both stamps
  back to back in one process. Same container, 10,240 rooms, the same mix over 10s:

    messages in the stamp    29 walks / 29 requests   1.00   62.4 ms mean  58.66 ms median
    structural stamp only     4 walks / 29 requests   0.14    8.7 ms mean   0.21 ms median

  Four walks in ten seconds is ceil(10 / ROOMS_CACHE_SECONDS) — the clock, exactly. The
  median is the answer to "what does a /rooms request cost now": 0.21 ms, one counter read,
  because 25 of the 29 hit. The mean is those four walks amortised over all of them. The
  walk count is stable run to run; the milliseconds move ~10% with the host, as above.

The two numbers worth watching are the last pair. A sync handler costs the loop nothing
because Starlette runs it in a threadpool; an `async def` handler that calls blocking store
code stalls every other request for the duration, and at a full store that duration is the
reap. test_app.py pins that as a regression test with a fake slow store.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import didkey  # noqa: E402
import store  # noqa: E402

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def bench(label: str, fn, rounds: int = 5, setup=None) -> None:
    """Best-of, not mean: these are dominated by syscalls against the page cache, and the
    minimum is the one that answers "what does this cost when nothing else is wrong"."""
    times = []
    for _ in range(rounds):
        if setup:
            setup()
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
    spread = f"±{statistics.pstdev(times):.0f}" if rounds > 1 else ""
    print(f"  {label:<38} {min(times):7.1f} ms  {spread}")


def build(root: Path, scale: float) -> None:
    rooms = max(2, int(store.MAX_ROOMS * scale))
    notes_per_ns = 512
    namespaces = max(1, int(store.MAX_NOTES_TOTAL * scale) // notes_per_ns)
    print(f"building {rooms} rooms and {namespaces * notes_per_ns} notes in {root} …")
    (root / "rooms").mkdir(parents=True, exist_ok=True)
    record = json.dumps({"seq": 1, "ts": 1, "from": "bench", "text": "x" * 120}) + "\n"
    for i in range(rooms):
        (root / "rooms" / f"r{i}.jsonl").write_text(record)
    for n in range(namespaces):
        ns = root / "notes" / f"ns{n}"
        ns.mkdir(parents=True, exist_ok=True)
        for k in range(notes_per_ns):
            (ns / f"k{k}.txt").write_text("v")


def store_bench(root: Path) -> None:
    fresh_room = root / "rooms" / "does-not-exist-yet.jsonl"
    fresh_note = root / "notes" / "ns0" / "does-not-exist-yet.txt"

    print("\ncreate path — paid once per NEW room or note, under the create gate")

    def room_gate() -> None:
        try:
            store._check_room_capacity(root, fresh_room)
        except store.StoreError:
            pass  # at the cap the refusal is the answer; the walk is what we are timing

    def note_gate() -> None:
        try:
            store._check_note_capacity(root, root / "notes" / "ns0", fresh_note)
        except store.StoreError:
            pass

    bench("_check_room_capacity", room_gate)
    bench("_check_note_capacity", note_gate)

    print("\nwrite path — a reap pass, at most once per REAP_EVERY")
    bench("_reap", lambda: store._reap(root), setup=lambda: _unlink(root / ".reaped"))

    print("\nread path — what /rooms costs when the cache misses")
    bench("room_stats(limit=200)", lambda: store.room_stats(root, limit=200))
    bench("note_stats", lambda: store.note_stats(root))

    print("\nwalk primitives — glob vs scandir, same store, back to back")
    bench("glob  rooms/*.jsonl", lambda: _drain(root.glob("rooms/*.jsonl")))
    bench("_walk rooms .jsonl", lambda: _drain(store._walk(root / "rooms", ".jsonl")))
    bench("glob  notes/*/*.txt", lambda: _drain(root.glob("notes/*/*.txt")))
    bench("_walk notes .txt", lambda: _drain(store._walk(root / "notes", ".txt")))
    bench("_scan notes .txt (count only)", lambda: _scan_notes(root))


def _did(i: int) -> str:
    """A distinct, real did:key — a shape the verifier would reject measures nothing."""
    n = int.from_bytes(didkey.MULTICODEC_ED25519 + i.to_bytes(4, "big") + b"\x00" * 28)
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58[rem] + out
    return f"{didkey.PREFIX}z{B58[0] * (didkey.MULTIBASE_CHARS - 1 - len(out))}{out}"


def _build_nonce_room(root: Path, room: str, records: int, mention: str | None = None) -> Path:
    """One signed record per distinct DID — the shape that makes the nonce scan expensive.
    `mention` quotes a DID in every record's *text*: legal, not that DID's nonce, and the
    case a bytes-level pre-filter matches wrongly."""
    path = store.room_path(root, room)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for seq in range(1, records + 1):
            text = f"@{mention} ack {seq}" if mention else "the quick brown fox jumps " + "x" * 25
            rec = {"seq": seq, "ts": store._now(), "from": _did(seq), "text": text, "nonce": seq}
            f.write(json.dumps(rec).encode() + b"\n")
    return path


def _parse_every(root: Path, room: str, did: str) -> int | None:
    """`_last_nonce` before the bytes-level reject. Kept here because a baseline that only
    exists in git history stops being run."""
    with store.room_path(root, room).open("rb") as f:
        for raw in store.reverse_lines(f):
            rec = store._parse(raw)
            if rec is not None and rec.get("from") == did and isinstance(rec.get("nonce"), int):
                return rec["nonce"]
    return None


def _scan_only(root: Path, room: str) -> None:
    """The floor: read the window backwards and parse nothing."""
    with store.room_path(root, room).open("rb") as f:
        for _ in store.reverse_lines(f):
            pass


def nonce_bench(root: Path, records: int) -> None:
    """A predicate scan, not a tail read: a DID that has NOT posted lately costs the whole
    READ_BUDGET, which is the common case (lobby: 826 signed writes/min, 770 distinct DIDs,
    a ~5,400-record window). It holds the room lock, so every signed write pays it. Both
    loops run over one file, so only the loop differs."""
    room, absent = "nonce-bench", _did(records + 5_000)
    path = _build_nonce_room(root, room, records)
    size = path.stat().st_size
    print(
        f"\nsigned-write path — _last_nonce over {records} records, {size >> 10} KiB "
        f"({size // records} B each); READ_BUDGET covers ~{store.READ_BUDGET // (size // records)}"
    )
    assert didkey.is_did(absent), absent  # the shape claim, not a hope
    assert store._last_nonce(root, room, absent) is None
    bench("absent DID  scan-only (the floor)", lambda: _scan_only(root, room), rounds=20)
    bench("absent DID  parse every record", lambda: _parse_every(root, room, absent), rounds=20)
    bench("absent DID  bytes reject first", lambda: store._last_nonce(root, room, absent), 20)

    recent = _did(records - 2)
    bench("recent DID  parse every record", lambda: _parse_every(root, room, recent), rounds=200)
    bench("recent DID  bytes reject first", lambda: store._last_nonce(root, room, recent), 200)

    # The adversarial shape: every line quotes the DID looked up, so every match is a false
    # positive and the filter is pure overhead. Bounded by the same budget, but measured.
    quoted = _did(7_777_777)
    _build_nonce_room(root, room, records, mention=quoted)
    assert store._last_nonce(root, room, quoted) is None  # a mention is not a `from`
    bench("quoted DID  parse every record", lambda: _parse_every(root, room, quoted), rounds=20)
    bench("quoted DID  bytes reject first", lambda: store._last_nonce(root, room, quoted), 20)
    path.unlink()


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def rooms_cache_bench(root: Path, seconds: float = 6.0) -> None:
    """Walks per /rooms request under write load, with and without `messages` in the cache
    stamp. Both halves run back to back in one process against one store, so host drift
    cancels — the same shape as the 0.9.1/0.9.2 comparison above.

    The axis is the write rate, which is why no other bench here finds this: `room_stats`
    costs the same either way, and what changed is how often /rooms has to call it. A walk
    is counted at the call, not inferred from a latency, so the figure is exact.

    It drives `app._rooms_view` rather than the route, so it measures the stamp alone. The
    `_rooms_cache.clear()` that used to run on every write in `take` cost the same thing
    per worker, and is gone for the same reason; a server-level run (`--port`) is what shows
    the two together.
    """
    import app
    import config

    # technocore.chat under live load. notes_per_sec is the axis this bench was missing:
    # production writes ~8 notes/sec and ~0.05 of them are topics, so a stamp that keys on
    # notes_written turns over ~24x per 3s window even with `messages` already out of it.
    messages_per_sec, rooms_per_sec, notes_per_sec = 24.0, 2.85, 8.0
    pool = min(512, _drain((root / "rooms").glob("r*.jsonl"))) or 1

    def run(label: str, keys: tuple) -> None:
        walks, latencies, sent, served, noted = 0, [], 0, 0, 0
        last: dict | None = None
        app._rooms_cache.clear()
        app.ROOMS_STAMP_KEYS = keys
        start = time.monotonic()
        while (now := time.monotonic() - start) < seconds:
            if sent / messages_per_sec <= now:
                store.append(root, f"r{sent % pool}", "bench", f"m{sent}")
                sent += 1
            if noted / notes_per_sec <= now:
                # A non-topic namespace, which is what production's note traffic is:
                # `did` and friends outnumber topic writes ~400:1.
                store.note_set(root, "did", f"k{noted % 4096:04x}", f"v{noted}")
                noted += 1
            if served / rooms_per_sec <= now:
                at = time.perf_counter()
                view = app._rooms_view(50)
                latencies.append((time.perf_counter() - at) * 1000)
                # Identity, not a wrapper around room_stats: a walk builds a fresh dict and
                # a hit returns the cached one, so this counts walks without patching the
                # store out from under the thing being measured.
                walks += view is not last
                last, served = view, served + 1
            time.sleep(0.001)
        # Median as well as mean: with the cache working most requests are hits, so the
        # median IS the hit — one counter read — and the mean is the few walks amortised.
        mid = statistics.median(latencies) if latencies else 0.0
        mean = sum(latencies) / len(latencies) if latencies else 0.0
        print(
            f"  {label:<30} {walks:3d} walks / {served:3d} requests   "
            f"{walks / max(served, 1):.2f} per request   {mean:5.1f} ms mean, "
            f"{mid:5.2f} ms median"
        )

    print(
        f"\n/rooms cache — {seconds:.0f}s at {messages_per_sec:g} messages/s into {pool} rooms "
        f"and {rooms_per_sec:g} /rooms/s, ROOMS_CACHE_SECONDS={config.ROOMS_CACHE_SECONDS:g}"
    )
    stamped = app.ROOMS_STAMP_KEYS
    # The periodic passes are throttled off these two markers. A reap inside one append
    # would land its 630 ms and its counter bump in one half of a six-second window.
    (root / ".reaped").touch()
    (root / store.SNAPSHOTS_FILE).touch()
    try:
        # config.ROOT is bound at import, and this script imports store (hence config) long
        # before it knows where the store is. Without this both halves walk /data.
        with config.override(ROOT=root):
            structural = ("rooms_created", "reaped_idle", "reaped_stillborn")
            run("0.9.3: messages + notes", ("messages", *structural, "notes_written"))
            run("0.9.4: notes_written", (*structural, "notes_written"))
            run("proposed: topics_written", (*structural, "topics_written"))
    finally:
        app.ROOMS_STAMP_KEYS = stamped
    print(
        "  the second row is set by the clock, not the traffic: one walk per "
        "ROOMS_CACHE_SECONDS\n  however hard /rooms is polled, so it falls further the "
        "busier the service gets"
    )


def _drain(iterator) -> int:
    return sum(1 for _ in iterator)


def _scan_notes(root: Path) -> int:
    total = 0
    for ns in (root / "notes").iterdir():
        if ns.is_dir():
            total += store._scan(ns, ".txt")[0]
    return total


def _get(url: str) -> float:
    start = time.perf_counter()
    subprocess.run(["curl", "-s", "-o", "/dev/null", url], check=False)
    return (time.perf_counter() - start) * 1000


def http_bench(port: int, room: str) -> None:
    base = f"http://127.0.0.1:{port}"
    print(f"\nhttp — against {base}, writing to /r/{room}")

    # An EXISTING room on purpose. At the cap a new one is refused, and an append is what
    # this should time regardless: the reap runs inside append either way, and appending is
    # what the service spends its life doing.
    subprocess.run(["curl", "-s", "-o", "/dev/null", f"{base}/r/{room}/say/b/hi"], check=False)
    # Cold because nothing has asked yet, not because of the write above: a message does not
    # end the cache window any more (see app._rooms_stamp). Run this against a fresh server.
    print(f"  {'GET /rooms cold':<38} {_get(base + '/rooms'):7.1f} ms")
    cached = min(_get(base + "/rooms") for _ in range(3))
    print(f"  {'GET /rooms cached':<38} {cached:7.1f} ms")

    print("\n  event loop unserved during one write (probing /healthz, which touches no disk)")
    for label, cmd in (
        ("GET /r/<room>/say/... (sync)", f"curl -s -o /dev/null '{base}/r/{room}/say/b/x'"),
        (
            "POST /r/<room> (async)",
            f"curl -s -o /dev/null -X POST -H 'Content-Type: application/json' "
            f'-d \'{{"from":"b","text":"x"}}\' \'{base}/r/{room}\'',
        ),
    ):
        print(f"  {label:<38} {_loop_stall(base, cmd):7.1f} ms")


def _loop_stall(base: str, write_cmd: str) -> float:
    """Worst /healthz latency observed while one write runs. A sync endpoint should cost the
    loop nothing; an async one that blocks shows the whole write here."""
    import threading

    latencies: list[float] = []
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            latencies.append(_get(base + "/healthz"))

    thread = threading.Thread(target=probe)
    thread.start()
    time.sleep(0.3)
    subprocess.run(write_cmd, shell=True, capture_output=True, check=False)
    time.sleep(0.3)
    stop.set()
    thread.join()
    return max(latencies) if latencies else float("inf")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, default=1.0, help="fraction of the caps to build")
    parser.add_argument("--keep", help="build here and leave it (share it with a server)")
    parser.add_argument(
        "--records", type=int, default=8255, help="records in the room the nonce scan reads"
    )
    parser.add_argument(
        "--seconds", type=float, default=6.0, help="window each /rooms cache half is driven for"
    )
    parser.add_argument("--port", type=int, help="also measure a server already on this port")
    parser.add_argument("--room", default="r0", help="an EXISTING room for --port to write to")
    args = parser.parse_args()

    root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="capacity-bench-"))
    print(
        f"caps: MAX_ROOMS={store.MAX_ROOMS} MAX_NOTES_TOTAL={store.MAX_NOTES_TOTAL} "
        f"MAX_TOTAL_ROOM_BYTES={store.MAX_TOTAL_ROOM_BYTES >> 20} MiB  scale={args.scale}"
    )
    try:
        if not (root / "rooms").exists():
            build(root, args.scale)
        store_bench(root)
        rooms_cache_bench(root, args.seconds)
        nonce_bench(root, args.records)
        if args.port:
            http_bench(args.port, args.room)
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    print(f"\n(store {'kept at ' + str(root) if args.keep else 'removed'})")


if __name__ == "__main__":
    os.environ.setdefault("CHAT_ROOT", "/tmp/unused-by-this-script")
    main()
