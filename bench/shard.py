"""Why the store shards one level wide, and why that level is 256.

Run: uv run python bench/shard.py

Sharding exists to stop one directory getting enormous. At the caps this service enforces,
a namespace at CHAT_MAX_NOTES_PER_NS is 200,000 directory entries counting the sidecar
lock beside every note, and `_check_note_capacity` scans that on every create in it. That is
the number to fix. Everything below is about not overshooting it.

Three measurements.

1. Resolution. `_shard` is memoized, so a name resolved before costs an lru_cache hit rather
   than a hash. The acceptance target is under 250 ns, which the raw hash alone does not meet
   and the cache comfortably does. Read the warm figure as "a working set that fits": one
   cache of MAX_ROOMS entries serves room names AND note keys, so traffic over a set larger
   than it evicts its way back to the cold cost, which the last row measures. The cache is
   sized from CHAT_MAX_ROOMS, so the middle row thrashes when this file is run without that
   set — which is itself the point being made. Still cheap against the stat that follows it,
   which costs microseconds.

2. Distribution. Deterministic hashing is only worth anything if it spreads. `digest_size=1`
   is 8 bits, which is exactly 256 — no mask, no slice, and no room for a modulo bias. Widths
   that do NOT divide the digest space are where that goes wrong, so the bias check is run
   against several to show what it looks like when it bites.

3. Width. The one that decided the layout. A wider shard makes buckets smaller and makes
   MORE directories, and past a point the second cost is real while the first has stopped
   buying anything: readdir does not care about the difference between 780 entries and 390.
   Two levels of 256 was the original design and is measured here as the thing it lost to.

Builds its own store in a tempfile directory — never read a real one.
"""

from __future__ import annotations

import hashlib
import shutil
import statistics
import sys
import tempfile
import timeit
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import store  # noqa: E402

ROOMS = 20_000  # the cap this was sized against, not the 5,120 default
PER_NS = 100_000  # CHAT_MAX_NOTES_PER_NS: the biggest one directory is allowed to get


def _names(n: int, prefix: str = "room") -> list[str]:
    return [f"{prefix}-{i:06d}" for i in range(n)]


def resolution() -> None:
    names = _names(ROOMS)
    store._shard.cache_clear()
    cold = timeit.timeit(lambda: [store._shard(n) for n in names], number=1) / len(names)
    warm = timeit.timeit(lambda: [store._shard(n) for n in names], number=3) / (3 * len(names))
    one = names[0]
    hit = timeit.timeit(lambda: store._shard(one), number=200_000) / 200_000
    over = _names(store.MAX_ROOMS * 8, "key")
    thrash = timeit.timeit(lambda: [store._shard(n) for n in over], number=1) / len(over)

    print("resolution")
    print(f"  _shard, cold (first sight of a name)   {cold * 1e9:8.1f} ns")
    print(f"  _shard, warm over {ROOMS:,} names ({store.MAX_ROOMS:,} cache) {warm * 1e9:6.1f} ns")
    print(f"  _shard, one hot name                   {hit * 1e9:8.1f} ns   <- the 250 ns target")
    print(f"  _shard, working set 8x the cache       {thrash * 1e9:8.1f} ns")
    verdict = "PASS" if hit * 1e9 < 250 else "FAIL"
    print(f"  {verdict}: {hit * 1e9:.0f} ns against a 250 ns budget, on a warm working set.")
    print(
        f"        A set that outgrows the cache costs {thrash * 1e9:.0f} ns — the hash, not a hit."
    )


def distribution() -> None:
    loads = list(Counter(store._shard(n) for n in _names(ROOMS)).values())
    expected = ROOMS / 256
    chi2 = sum((load - expected) ** 2 / expected for load in loads)
    print(f"\ndistribution of {ROOMS:,} names over 256 buckets")
    print(f"  buckets occupied                       {len(loads):8d} / 256")
    print(
        f"  mean / min / max load                  {statistics.mean(loads):8.1f}"
        f" / {min(loads)} / {max(loads)}"
    )
    # chi2 over 255 degrees of freedom: ~1.0 is what a uniform hash gives.
    print(f"  chi-square / df (1.00 is uniform)      {chi2 / 255:8.2f}")

    print("\n  bias check — a width must divide the digest space it is taken from:")
    for width, space in ((256, 256), (512, 65536), (500, 65536), (1000, 65536)):
        spread = Counter(v % width for v in range(space)).values()
        flag = "clean" if min(spread) == max(spread) else "BIASED"
        print(
            f"    {width:>5} buckets from {space:>5} digests: {min(spread)}-{max(spread)} each  {flag}"
        )


def _occupied(n: int, width: int) -> float:
    """How many of `width` buckets `n` names land in, if the hash is uniform."""
    return width * (1 - (1 - 1 / width) ** n) if n else 0.0


def width() -> None:
    # The live shape (technocore.chat, 2026-08-26): ~1,000 of 1,346 namespaces hold five
    # notes or fewer, and the busiest holds the per-namespace cap. The tail is what makes a
    # deep shard expensive — every near-empty namespace still pays for its own bucket tree.
    tail = [1] * 839 + [3] * 170 + [12] * 33 + [50] * 22
    big = [PER_NS] * 6 + [3000] * 267
    sizes = tail + big
    total = sum(sizes)
    print(f"\nwidth, against the live namespace shape scaled to {total:,} notes")
    print(f"  {'layout':<26}{'directories':>13}{'per file':>10}{'busiest bucket':>17}")
    for label, dirs, busiest in (
        ("flat (today)", len(sizes), PER_NS),
        ("1 level of 256 (this)", sum(1 + _occupied(n, 256) for n in sizes), PER_NS / 256),
        ("1 level of 512", sum(1 + _occupied(n, 512) for n in sizes), PER_NS / 512),
        (
            "2 levels of 256",
            sum(1 + _occupied(n, 256) + _occupied(n, 65536) for n in sizes),
            PER_NS / _occupied(PER_NS, 65536),
        ),
    ):
        print(f"  {label:<26}{dirs:>13,.0f}{dirs / total:>10.3f}{busiest * 2:>17,.0f}")
    print("  (busiest bucket counts the .lock sidecar beside every note)")


def _time_walks(rooms: Path) -> tuple[float, float]:
    """Bound to `rooms` as an argument, never closed over: a lambda capturing the loop
    variable below would time whichever layout the loop had reached by then."""
    walk = timeit.timeit(lambda: sum(1 for _ in store._walk(rooms, ".jsonl")), number=3)
    scan = timeit.timeit(lambda: store._scan(rooms, ".jsonl", sized=True), number=3)
    return walk, scan


def walks() -> None:
    """The cost the reaper, /rooms, /stats and every room create actually pay."""
    print("\nwalks over a rooms/ directory of 20,000")
    for label, sharded in (("flat", False), ("1 level of 256", True)):
        root = Path(tempfile.mkdtemp())
        try:
            for name in _names(ROOMS):
                d = root / "rooms" / store._shard(name) if sharded else root / "rooms"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{name}.jsonl").write_text("{}\n")
                (d / f"{name}.jsonl.lock").touch()  # the sidecar every real room carries
            walk, scan = _time_walks(root / "rooms")
            print(
                f"  {label:<16} _walk {walk / 3 * 1e3:7.2f} ms   "
                f"_scan(sized) {scan / 3 * 1e3:7.2f} ms"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    assert hashlib.blake2b(b"x", digest_size=1).hexdigest() == store._shard("x"), (
        "the benchmark and the store disagree about the layout"
    )
    resolution()
    distribution()
    width()
    walks()
