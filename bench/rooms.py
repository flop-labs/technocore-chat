#!/usr/bin/env python3
"""What a /rooms walk costs, and which half of it `limit` actually buys.

Run: uv run python bench/rooms.py            (add --help for the knobs)

`store.room_stats` does two things with very different shapes, and every question about
/rooms turns on which of them dominates:

1. **The walk.** One readdir over the room directory and one stat per room, to sort by
   recency and total the bytes. O(rooms on disk); `limit` does not touch it.
2. **The windows.** For the `limit` rooms actually shown, one bounded backwards read of
   each room's tail (WINDOW_BYTES, WINDOW_MESSAGES) for `last_seq` and the engagement
   aggregates. O(limit), memoized per room on `(st_mtime_ns, st_size)`.

That memo is why the arms here matter more than the ladder. A room's entry dies the moment
the room is written to, so the *recently active* rooms — exactly the ones a recency-sorted
listing shows first — are the ones whose windows are least likely to be memoized. `cold` is
that case, `warm` is an idle store, and production sits between them. Which end it sits
nearer is what decides whether serving a smaller `limit` buys anything at all.

Three things about how this is measured, each of which changes the number:

1. **`store.room_stats`, not the endpoint.** The handler adds note stats and a rendering and
   sits behind `_rooms_walk`'s LRU, which is keyed on `limit`; timing it through HTTP
   measures that cache and the CDN in front of it. The walk is what this file is about.

2. **The ladder runs ascending, and each point clears what it means to clear.** Measured
   descending, every point warms the memo for the next and the cheap end reads as nearly
   free. A curve collected that way cannot separate the walk from the windows, which is the
   one thing it is collected to do.

3. **The store is shaped like the real one, not filled uniformly.** Most rooms hold a
   handful of records; a small head holds enough tail to fill a window. A store where every
   room is deep overstates the windows, one where none are understates them. `--rooms` and
   `--deep` are the two halves, and the head is written last so it sorts first.

`--writers` adds threads appending to the head of the listing while the walk runs. It is not
a proposal to change anything: it separates the walk's own work from time spent waiting on
the per-room locks a writer holds, which no single-threaded arm can do.

Builds its own store in a tempfile directory — never point it at a real one.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

# Setup writes tens of thousands of records and the walk never fsyncs, so durability here
# would only measure the setup. Set before `store` is imported: config reads env at import.
os.environ.setdefault("CHAT_FSYNC", "0")


def _requested(flag: str, argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


# --rooms is read here rather than from the parsed args, because MAX_ROOMS is an import-time
# read of CHAT_MAX_ROOMS and the room-creation gate refuses past it: a ladder asking for more
# rooms than the cap does not measure a bigger store, it dies in setup. The deployed cap is a
# config value and not the code default, and since the walk is O(rooms on disk) it is the
# single knob that decides what this benchmark is measuring. Raise both together or neither.
_rooms = _requested("--rooms", sys.argv[1:])
if _rooms:
    os.environ["CHAT_MAX_ROOMS"] = _rooms

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

import store  # noqa: E402

LADDER = (1, 10, 25, 50, 100, 150, 200)


def build(root: Path, rooms: int, deep: int, depth: int) -> float:
    """A store of `rooms` rooms, the newest `deep` of them holding `depth` records each.

    The deep rooms are written last, so they are the ones a recency-sorted listing reaches
    first. That ordering is the whole point: a store whose deep rooms sort last would let
    every limit below `rooms - deep` skip the tail reads entirely.
    """
    (root / "rooms").mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    for i in range(max(0, rooms - deep)):
        store._write_record(root, f"bench-s{i:06d}", "n0", "x")
    for i in range(deep):
        name = f"bench-d{i:06d}"
        for j in range(depth):
            store._write_record(root, name, f"n{j % 7}", "m" * 96)
    return time.perf_counter() - t


def once(root: Path, limit: int, cold: bool) -> float:
    """One `room_stats` call. `cold` drops the window memo and nothing else — `_listable`
    and the topic memo stay warm because in production they are: they survive writes."""
    if cold:
        store._cached_window.cache_clear()
    t = time.perf_counter()
    store.room_stats(root, limit=limit)
    return time.perf_counter() - t


def arm(root: Path, limit: int, cold: bool, reps: int) -> dict[str, float]:
    if not cold:
        store._cached_window.cache_clear()
        store.room_stats(root, limit=limit)  # populate the memo; not measured
    s = sorted(once(root, limit, cold) for _ in range(reps))
    return {"median": statistics.median(s), "min": s[0], "max": s[-1]}


def contended(root: Path, limit: int, reps: int, writers: int, deep: int) -> dict[str, float]:
    """The cold arm again, with `writers` threads appending to the head of the listing.

    Writers move the mtime of the rooms they touch, so they invalidate exactly the memo
    entries the walk is about to want. That is not an artefact of the harness — it is the
    production case, and it is why the contended arm is not simply the cold arm plus lock
    wait.
    """
    stop = threading.Event()

    def churn(k: int) -> None:
        while not stop.is_set():
            store._write_record(root, f"bench-d{k:06d}", "w", "churn")

    threads = [
        threading.Thread(target=churn, args=(i,), daemon=True)
        for i in range(min(writers, max(1, deep)))
    ]
    for t in threads:
        t.start()
    try:
        return arm(root, limit, True, reps)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)


@dataclasses.dataclass
class Row:
    """One ladder point. The arms stay a dict because `--writers` decides how many there
    are, but `limit` is its own field: it is arithmetic everywhere it is used."""

    limit: int
    arms: dict[str, dict[str, float]]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--rooms", type=int, default=store.MAX_ROOMS, help="rooms on disk (default: the cap)"
    )
    p.add_argument("--deep", type=int, default=max(LADDER), help="how many hold a full window")
    p.add_argument(
        "--depth", type=int, default=store.WINDOW_MESSAGES, help="records in a deep room"
    )
    p.add_argument("--reps", type=int, default=5, help="timed calls per point")
    p.add_argument("--writers", type=int, default=0, help="concurrent appenders, cold arm")
    p.add_argument("--limits", type=int, nargs="+", default=list(LADDER))
    p.add_argument("--json", action="store_true", help="emit the rows as JSON, not a table")
    args = p.parse_args()

    with tempfile.TemporaryDirectory(prefix="bench-rooms-") as tmp:
        root = Path(tmp)
        setup = build(root, args.rooms, args.deep, args.depth)
        rows: list[Row] = []
        for limit in sorted(args.limits):  # ascending: see the docstring
            arms = {
                "cold": arm(root, limit, True, args.reps),
                "warm": arm(root, limit, False, args.reps),
            }
            if args.writers:
                arms["contended"] = contended(root, limit, args.reps, args.writers, args.deep)
            rows.append(Row(limit, arms))

        meta = {
            "rooms": args.rooms,
            "deep": args.deep,
            "depth": args.depth,
            "reps": args.reps,
            "writers": args.writers,
            "window_bytes": store.WINDOW_BYTES,
            "window_messages": store.WINDOW_MESSAGES,
            "setup_seconds": round(setup, 1),
        }
        if args.json:
            payload = [{"limit": r.limit, **r.arms} for r in rows]
            print(json.dumps({"meta": meta, "rows": payload}, indent=2))
            return 0

        print(
            f"{args.rooms} rooms, newest {args.deep} holding {args.depth} records "
            f"(built in {setup:.1f}s); {args.reps} reps per point\n"
        )
        cols = ["cold", "warm"] + (["contended"] if args.writers else [])
        print("limit  " + "".join(f"{c:>12}" for c in cols))
        for r in rows:
            print(
                f"{r.limit:>5}  " + "".join(f"{r.arms[c]['median'] * 1000:>10.1f}ms" for c in cols)
            )

        # The limit-independent floor: at limit=1 the windows are one room, so what is left
        # is the readdir and one stat per room. Everything above it is what `limit` buys.
        if rows[0].limit == 1:
            floor_ms = rows[0].arms["cold"]["median"] * 1000
            top = rows[-1]
            print(
                f"\nwalk floor (limit=1, cold): {floor_ms:.1f}ms of the "
                f"{top.arms['cold']['median'] * 1000:.1f}ms at limit={top.limit} — the rest "
                f"is {top.limit - 1} window reads."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
