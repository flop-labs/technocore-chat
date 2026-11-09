"""What the per-room append critical section costs, measured at `store.append` end to end.

Run: uv run python bench/append.py            (add --help for the knobs)

An append to an *existing* room holds one lock — that room's own flock — so everything
inside the section is time every other writer to that room waits. On production that section
was 41% of worker thread-time (py-spy, 0.11.4, 12 workers x 40 anyio threads), and one room
carried ~29 writes/s through it.

Three things about how this is measured, each of which changes the number:

1. **A realistically sized room.** The section holds two reads whose cost depends on file
   size — `last_seq`'s backward scan and, on the signed lane, `_last_nonce`'s. Benchmarking
   an empty room measures neither. The default preload builds ~6 MB, which is what a busy
   room looks like since 0.11.4 derived COMPACT_MAX_LINES from MAX_ROOM_BYTES.

2. **Contended as well as single-threaded.** Single-threaded latency is what the section
   costs; the contended number is what it costs *everyone else*, and they move differently —
   removing 30 us from a section eight threads are queued on is worth more than 30 us.

3. **Not compaction.** The preload leaves the room below the compaction trigger on purpose.
   `_compact` is a ~150-400 ms stall every ~25 min on a hot room; averaging it into a
   per-append figure hides both the stall and the steady-state cost. Measure it separately.

CHAT_FSYNC=0 to match the deployment. On a local filesystem the absolute numbers are not
production's — flock, page cache and disk all differ — so read the ratio, not the value.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("CHAT_FSYNC", "0")

import store  # noqa: E402

TEXT = "benchmark message body of roughly typical length for this service"


def _fill(root: Path, room: str, n: int) -> None:
    for i in range(n):
        store.append(root, room, "bot", f"{TEXT} {i}")


def _single(root: Path, room: str, warm: int, n: int) -> list[float]:
    for _ in range(warm):
        store.append(root, room, "bot", TEXT)
    out = []
    for _ in range(n):
        t = time.perf_counter()
        store.append(root, room, "bot", TEXT)
        out.append((time.perf_counter() - t) * 1e6)
    return out


def _contended(root: Path, room: str, threads: int, per_thread: int) -> tuple[list[float], float]:
    lat: list[float] = []
    guard = threading.Lock()

    def worker() -> None:
        mine = []
        for _ in range(per_thread):
            t = time.perf_counter()
            store.append(root, room, "bot", TEXT)
            mine.append((time.perf_counter() - t) * 1e6)
        with guard:
            lat.extend(mine)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    start = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return lat, (threads * per_thread) / (time.perf_counter() - start)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default="append", help="printed with the row, for A/B runs")
    ap.add_argument("--preload", type=int, default=45_000, help="records written before timing")
    ap.add_argument("--warm", type=int, default=300)
    ap.add_argument("--n", type=int, default=3_000, help="timed single-threaded appends")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--per-thread", type=int, default=500)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _fill(root, "hot", args.preload)
        mb = store.room_path(root, "hot").stat().st_size / 1e6
        single = _single(root, "hot", args.warm, args.n)
        many, throughput = _contended(root, "hot", args.threads, args.per_thread)

    p90 = lambda xs: statistics.quantiles(xs, n=10)[8]  # noqa: E731
    print(
        f"{args.label:>10} | room {mb:5.2f} MB "
        f"| single p50 {statistics.median(single):7.1f}us p90 {p90(single):8.1f}us "
        f"| {args.threads}-thread p50 {statistics.median(many):7.1f}us p90 {p90(many):8.1f}us "
        f"| {throughput:6.0f} appends/s"
    )


if __name__ == "__main__":
    main()
