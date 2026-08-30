"""Why `_walk` yields `os.DirEntry` and the orphan sweep never builds a Path.

Run: uv run python bench/dir_walk.py

The reaper makes full-tree passes on the write path, throttled to REAP_EVERY. At the live
caps that is 10,240 room files and ~200,000 note files, each with a `.lock` sidecar beside
it — ~420,000 directory entries per pass, every one of which used to become a `pathlib.Path`.

The cost is not where a reader looks for it. On 3.12 pathlib is lazily normalised: the
constructor stashes the raw string and the parse lands on the first `__fspath__`, which for
this code is `.stat()`. So `Path(p)` alone looks cheap and `Path(p).stat()` costs about
2.5x the syscall it wraps, with the overhead hidden inside the stat call.

Two things this measures that bound the fix:

- No `os.*` spelling of the stat is meaningfully faster. `DirEntry.stat()`, `os.stat(path)`
  and `os.stat(name, dir_fd=)` land within ~1.2x of each other and Path is 2.5x off all
  three, so there is no cleverer syscall to reach for — the representation is the whole
  cost. That is also why the sweep uses a plain absolute path rather than threading a
  directory fd out of the walk: on the prod box dir_fd is 4.55 us against os.stat(path)'s
  5.55, a fifth of one microsecond per file against an fd lifetime per namespace.
- readdir alone is the floor: 15% of the as-written cost on the prod box, 24% on a fast
  desktop. That is what tells you the remaining time is the kernel doing stats rather than
  Python doing pathlib — and that no rewrite of this loop can go below it.

Builds its own store in a tempfile directory — never read a real one. 10,000 files is enough
to be stable and is close to the production rooms directory's 10,240.

The absolute microseconds move with the filesystem and the load, so the ratios are the claim
— but the headline ratio moves too, and predictably enough to say how. It is
`1 + pathlib_overhead / stat_cost`: pathlib is Python and scales with the CPU, the stat is a
syscall and scales with the filesystem, and the two do not move together. A fast desktop core
makes the Python half ~5x cheaper while making the syscall only ~3x cheaper, so it *shrinks*
the ratio the change is argued from. Measured both ways, same image, same 10,000 files:

                                  prod container   M4 Mac
    Path(e.path).stat()             12.14 us       2.81 us
    e.stat()                         4.81 us       1.59 us
    Path.stat() vs DirEntry.stat()   2.52x         1.77x
    sweep, as written               25.75 us       5.62 us
    sweep, os.access                 4.45 us       1.93 us
    sweep rewrite                    5.79x         2.90x

Prod there is the 4-core Vultr instance the service runs on, in the same image tag it was
serving, at load average 2.2/5.0/5.4 — i.e. genuinely contended, which is the condition the
reaper actually runs under. Quote those when arguing about production.

So this prints `Path(p)` construction on its own as a calibration line: pure Python, no
syscall, ~1.97 us on that box against ~0.36 us on the Mac. Read the walk ratios against
wherever your box lands between them. The ordering — Path slowest, every os.* spelling
clustered within ~1.2x, the sweep rewrite several-fold — is what holds everywhere, and it is
what the gate at the end checks alongside the looser desktop floors.
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time
from pathlib import Path

FILES = 10_000
BATCHES = 7


def per_call_us(work, n: int = 200_000) -> float:
    """Microseconds per call for the pure-Python calibration line — no filesystem in it."""
    work()
    batches = []
    for _ in range(BATCHES):
        start = time.perf_counter()
        for _ in range(n):
            work()
        batches.append((time.perf_counter() - start) / n * 1e6)
    return statistics.median(batches)


def per_file_us(work, root: str, n: int = FILES) -> float:
    """Microseconds per file, median of 7 full passes so one scheduling blip cannot win.

    The first pass is discarded: it is what warms the dentry and inode caches, and timing a
    cold tree measures the page cache rather than the code.
    """
    work(root)
    batches = []
    for _ in range(BATCHES):
        start = time.perf_counter()
        work(root)
        batches.append((time.perf_counter() - start) / n * 1e6)
    return statistics.median(batches)


def build(root: str) -> None:
    """`n` data files, each with the `.lock` sidecar the store keeps beside it.

    Half the locks are orphaned — their data file is removed — so the sweep predicate is
    measured on a mix rather than on whichever branch happens to short-circuit first.
    """
    for i in range(FILES):
        name = os.path.join(root, f"room{i:05d}.jsonl")
        with open(name, "wb") as f:
            f.write(b'{"seq":1,"from":"bot","text":"hi"}\n')
        with open(name + ".lock", "wb"):
            pass
        if i % 2:
            os.unlink(name)  # an orphan lock, the case the sweep exists for


# --- the walk: what one reap pass pays per data file -------------------------------------


def readdir_only(root: str) -> int:
    """The floor. No stat at all — just the entries readdir already returned."""
    n = 0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl"):
                n += 1
    return n


def path_stat(root: str) -> float:
    """As written before this change."""
    total = 0.0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl"):
                total += Path(e.path).stat().st_mtime
    return total


def direntry_stat(root: str) -> float:
    total = 0.0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl"):
                total += e.stat().st_mtime
    return total


def os_stat_path(root: str) -> float:
    """What `_reapable` does now. Never cached, which is the point — see its docstring."""
    total = 0.0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl"):
                total += os.stat(e.path).st_mtime
    return total


def os_stat_dir_fd(root: str) -> float:
    total = 0.0
    fd = os.open(root, os.O_RDONLY)
    try:
        with os.scandir(root) as entries:
            for e in entries:
                if e.name.endswith(".jsonl"):
                    total += os.stat(e.name, dir_fd=fd).st_mtime
    finally:
        os.close(fd)
    return total


# --- the sweep predicate: does this lock's data file still exist? -------------------------


def sweep_as_written(root: str) -> int:
    """`Path.with_suffix("").exists()` — two Path allocations per lock."""
    n = 0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl.lock") and Path(e.path).with_suffix("").exists():
                n += 1
    return n


def sweep_os_stat(root: str) -> int:
    """The exact equivalent of `.exists()`, without the Path."""
    n = 0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl.lock"):
                try:
                    os.stat(e.path[: -len(".lock")])
                    n += 1
                except OSError:
                    pass
    return n


def sweep_os_access(root: str) -> int:
    """What the sweep does now: a bool back instead of a discarded stat_result."""
    n = 0
    with os.scandir(root) as entries:
        for e in entries:
            if e.name.endswith(".jsonl.lock") and os.access(e.path[: -len(".lock")], os.F_OK):
                n += 1
    return n


def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        build(root)
        print(f"{FILES} data files + {FILES} lock sidecars in {root}")
        print(f"median of {BATCHES} full passes, first pass discarded\n")

        floor = per_file_us(readdir_only, root)
        walk = {
            "Path(e.path).stat()": per_file_us(path_stat, root),
            "e.stat()": per_file_us(direntry_stat, root),
            "os.stat(e.path)": per_file_us(os_stat_path, root),
            "os.stat(e.name, dir_fd=)": per_file_us(os_stat_dir_fd, root),
        }
        base = walk["Path(e.path).stat()"]
        print("the walk — one mtime per data file")
        print(f"  {'readdir only (floor)':<26} {floor:7.2f} us/file  {floor / base:5.2f}x")
        for label, us in walk.items():
            print(f"  {label:<26} {us:7.2f} us/file  {base / us:5.2f}x")

        sweep = {
            'Path.with_suffix("").exists()': per_file_us(sweep_as_written, root),
            "os.stat(sliced path)": per_file_us(sweep_os_stat, root),
            "os.access(sliced path)": per_file_us(sweep_os_access, root),
        }
        sbase = sweep['Path.with_suffix("").exists()']
        print("\nthe sweep predicate — does this lock's data file exist?")
        for label, us in sweep.items():
            print(f"  {label:<26} {us:7.2f} us/lock  {sbase / us:5.2f}x")

        # Pure Python, no syscall: the constant that says how to read the ratios above.
        ctor = per_call_us(lambda: Path("/data/notes/room-owners/somenamespacekey.txt"))
        print(f"\ncalibration: Path(p) construction, no syscall {ctor:6.3f} us")
        print("             (1.97 us on the prod box, 0.36 us on an M4 — scale the ratios)")

        # The gate, checked rather than eyeballed: a bench that cannot reproduce the
        # direction and rough size of the win is not evidence for the change it justifies.
        #
        # The floors are the desktop ones, not the container's 2.5x and 5.8x. Both ratios
        # have pure Python on top and a syscall underneath, so both compress on a fast core,
        # and the sweep compresses harder because `with_suffix` is the most Python-heavy
        # thing being removed (12.5 us on the container, 1.0 us here). Gating on the
        # container's numbers would fail every developer machine; gating on the ordering as
        # well is what keeps the looser floors honest, because the ordering is the claim.
        # Measured on the prod box under load: 2.52x and 5.79x, both clear of these floors.
        stat_ratio = base / walk["e.stat()"]
        sweep_ratio = sbase / sweep["os.access(sliced path)"]
        print(f"\nPath.stat() vs DirEntry.stat() {stat_ratio:.2f}x  (>= 1.7x here, 2.5x in prod)")
        print(f"sweep rewrite                 {sweep_ratio:.2f}x  (>= 2.5x here, 5.8x in prod)")
        print(f"readdir floor is {floor / base * 100:.0f}% of the as-written walk")
        # Path slowest in both groups, and the os.* spellings clustered rather than one of
        # them being the real win. Neither depends on the CPU or the filesystem.
        os_spellings = [walk["e.stat()"], walk["os.stat(e.path)"], walk["os.stat(e.name, dir_fd=)"]]
        ordered = base == max([floor, *walk.values()]) and sbase == max(sweep.values())
        clustered = max(os_spellings) / min(os_spellings) < 1.6
        if stat_ratio < 1.7 or sweep_ratio < 2.5 or not ordered or not clustered:
            print("\n!! outside the expected range — work out why before quoting these")


if __name__ == "__main__":
    main()
