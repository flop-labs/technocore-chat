"""What /rooms shows: the incremental rooms index, and the walk it falls back to.

Two files' worth of concern in one module because they are the same concern. `/rooms`
answers three questions about every listable room — recency order, a count, a byte total —
and there are exactly two ways to answer them: read the maintained index, or walk `rooms/`.
Splitting the two across modules put the fast path and its fallback in places that could
drift apart, and the review this code was written under found precisely that drift: an
index that was authoritative in one place and best-effort in another. Here the choice
between them is made once, in `listing()`, and both answers have the same type.

`extra`, not core: `sz.py`'s `EXTRA_FILES` reports this file without counting it against
the per-file ceiling that `just check` enforces on `core/store.py`. That ceiling was at
zero headroom, so a subsystem of this size could not have landed in core at all — see the
per-file caps in `sz-baseline.json` and CONTRIBUTING's line-tradeoff rule.

Circular import, deliberately. This module needs core's primitives (`_locked`, `_replace`,
`_listable`, `room_path`, `_walk`) and core's read/write paths need this module. The edge
is safe because neither side touches the other's attributes while the other is still
initialising: every use below happens inside a function body, at call time, by which point
both modules are complete. `manifest.py` already imports `store` the same way; this only
adds the reverse edge.

The index itself is append-only JSONL, one `{room, mtime, size}` line per write:

- **Written** by `observe()` on every `append()`, one line, never a rewrite — a read-
  modify-rewrite over the whole file would be O(rooms) per message, which is the cost this
  file exists to avoid.
- **Compacted** by `compact()`, which publishes a full snapshot atomically with every
  line appended since the snapshot's walk carried onto its tail, so an append and a
  publish cannot lose each other's work.
- **Read** by `listing()` down to a page, or by `read()` whole; a missing, truncated or
  unparseable file reads as empty and sends the caller to the walk rather than to a
  snapshot that is silently missing rooms.

Invariant worth stating once: **the index pathname exists only when it is complete.**
`observe()` will not create it, so a fresh store walks exactly as it did before this file
existed, until the first reaper pass seeds the index from a full walk. A one-line index on
a store holding hundreds of thousands of rooms would read as authoritative and hide every
room but one.
"""

from __future__ import annotations

import os
from pathlib import Path

import orjson

import store

ROOMS_INDEX_FILE = ".rooms-index"
# The quarantine target. Not `.rooms-index` with a suffix: `Path(".rooms-index").suffix` is
# "", so `with_suffix` would build `.corrupt` and orphan the two files beside each other.
# Appending to the literal name keeps the sidecar `.rooms-index.lock` its own thing.
CORRUPT_SUFFIX = ".corrupt"


def _path(root: Path) -> Path:
    return root / ROOMS_INDEX_FILE


def read(root: Path) -> dict[str, tuple[float, int]]:
    """`{room: (mtime, size)}`, or `{}` if the file cannot be trusted — fail closed.

    The file is append-only, so several lines per room are expected and the *last* wins
    (a dict overwrite). A compacted file and an un-compacted one with pending appends
    therefore both produce the current state.

    Fail-closed on corruption: `{}` when the file is missing **or** when any non-blank
    line fails to validate, because `{}` sends the caller to the walk. Skipping bad lines
    and keeping the rest was tried and rejected in review — a truncated file would then
    read as a *complete* index that happens to be missing rooms, and /rooms would silently
    omit real rooms, which is worse than paying for the walk. Blank trailing lines are
    fine; anything else unparseable poisons the whole file.
    """
    try:
        data = _path(root).read_bytes()
    except OSError:
        return {}  # no index yet — the walk fallback is the normal first-read path
    if not data:
        return {}
    index: dict[str, tuple[float, int]] = {}
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = orjson.loads(line)
            name = entry["room"]
            mtime = entry["mtime"]
            size = entry["size"]
            if not store._listable(name):
                continue  # well-formed but not a room this service would list
            if not (isinstance(mtime, (int, float)) and isinstance(size, int) and size >= 0):
                raise TypeError(name)
        except (ValueError, KeyError, TypeError):
            return {}  # one bad line poisons the whole file: fail closed to the walk
        index[name] = (mtime, size)
    return index


def observe(root: Path, room: str, mtime: float, size: int) -> None:
    """Append one entry for `room`. O(1), never a rewrite.

    The reader takes the last line per room, so duplicate lines are fine: one ~60-byte
    JSON object, folded away at the next compaction.

    Concurrency (the finding this version exists to fix): the append holds
    `ROOMS_INDEX_LOCK` **shared** while compaction holds it **exclusive** across its
    re-read and publish. An append is therefore either wholly before the compactor's
    re-read — its line is carried onto the staging file — or wholly after the replace,
    opening the new inode. There is no window in which a committed room write's index line
    is dropped. Shared rather than none, so appenders exclude only the publisher and never
    each other; no deadlock, because the exclusive holder takes no other lock while it
    holds this one.

    On any `OSError` while an index exists the file is renamed aside rather than left
    behind: a failed append means the index no longer describes the store, and a later
    read must walk instead of serving a snapshot that is silently missing rooms. The
    exists-check rides inside the same shared hold so a quarantine can never land between
    check and append and slip a partial file in behind itself.

    A file that is *already* gone is not re-created from this one line. Re-creating it
    here would break the invariant this module states outright — the pathname exists only
    when it is complete — and the breach is reachable, not theoretical: two appenders
    that both hit an unwritable index, where the first quarantines and the second then
    writes its single line into a store holding thousands of rooms. `read()` would return
    that one room as a complete index and `/rooms` would serve a page of one. Absent
    stays absent until a reap pass seeds it from a full walk.
    """
    path = _path(root)
    line = orjson.dumps({"room": room, "mtime": mtime, "size": size}) + b"\n"
    try:
        with store._locked(path, shared=True):
            if not path.exists():
                return  # no complete index yet: the first reap seeds it; /rooms walks
            with open(path, "ab") as f:
                f.write(line)
    except FileNotFoundError:
        pass  # raced a quarantine or a compact: gone; /rooms walks until the next seed
    except OSError:
        try:
            with store._locked(path):
                if path.exists():
                    path.rename(root / (ROOMS_INDEX_FILE + CORRUPT_SUFFIX))
                # else: already quarantined or compacted away. Nothing to do, and nothing
                # to write — see the docstring's note on not re-creating the pathname.
        except OSError:
            try:
                path.rename(root / (ROOMS_INDEX_FILE + CORRUPT_SUFFIX))
            except OSError:
                pass  # nothing left to do; the walk fallback answers for /rooms


def observe_file(root: Path, room: str) -> None:
    """`observe()` for a room that has just been written: stat it under its own lock.

    The lock is the room's, so the record's append and the index line describing it cannot
    interleave with the reaper's walk of the same room — the lock that protected the
    record now also orders the index entry. Best effort throughout: the caller's write has
    already succeeded by the time this runs, and a failure quarantines (see `observe`).
    """
    try:
        path = store.room_path(root, room)
        if not path.exists():
            return
        with store._locked(path):
            st = path.stat()
            observe(root, room, st.st_mtime, st.st_size)
    except OSError:
        pass


def collect(root: Path) -> dict[str, tuple[float, int]]:
    """`{room: (mtime, size)}` for every listable room, from one walk of `rooms/`.

    The full-rebuild path: what `compact()` uses when called without a snapshot. Walked
    here rather than shared with the reaper because `_reap_pass` builds the same dict
    inline from the stat its own walk already took and passes it in — a second pass over
    the tree is exactly what neither caller wants.

    `_listable`-filtered to match `listing()`'s own walk, so both paths agree on what a
    room is: unlisted rooms stay out of the index exactly as they stay out of /rooms.
    """
    index: dict[str, tuple[float, int]] = {}
    for e in store._walk(root / "rooms", ".jsonl"):
        name = e.name[: -len(".jsonl")]
        if not store._listable(name):
            continue
        try:
            st = e.stat()
            index[name] = (st.st_mtime, st.st_size)
        except OSError:
            continue  # reaped between the readdir and the stat
    return index


def compact(root: Path, index: dict[str, tuple[float, int]] | None = None) -> None:
    """Replace the index with one line per listable room, carrying everything newer.

    Without `index` this walks and stats itself — the rebuild path. With one (the
    reaper's) it publishes that snapshot and no second walk happens.

    Publish protocol, for the race the reviews caught: the snapshot is staged through
    `_replace`'s unique temp name and — still under `ROOMS_INDEX_LOCK` **exclusive** —
    everything appended to the *pathname* since the snapshot was taken is re-read and
    carried onto the staging file before the one atomic replace. Appenders hold the lock
    shared while writing, so one is either wholly inside the re-read window (its line is
    carried) or wholly after the replace (it opens the new inode). Between the two the
    views reconcile exactly; nothing is lost in either order.

    Never raises. A failed publish leaves the previous index in place and a reader either
    walks or serves that snapshot, so the failure costs a stale figure for one interval —
    whereas raising would turn a *finished reap* into a failed one, which is a much larger
    thing to lose. The reaper calls this at the end of every pass.
    """
    try:
        if index is None:
            index = collect(root)
        lines = sorted(
            (
                orjson.dumps({"room": r, "mtime": m, "size": s}).decode()
                for r, (m, s) in index.items()
            ),
            key=lambda s: (orjson.loads(s)["mtime"], s),
            reverse=True,
        )
        path = _path(root)
        # The snapshot is stale by construction: anything appended to the pathname since
        # the walk ran is newer than every line in it, and `_listable` was applied to
        # those lines by their writer. Carrying them onto the snapshot's tail is the whole
        # point of the publish step — dropping one would be the lost update the reviews
        # named. The reader's last-line-per-room dedupe folds any overlap away.
        carry: list[bytes] = []
        carried: set[str] = set()
        with store._locked(path):
            try:
                for raw in path.read_bytes().split(b"\n"):
                    if not raw.strip() or not raw.startswith(b'{"room":'):
                        continue
                    try:
                        entry = orjson.loads(raw)
                        name = entry["room"]
                    except (ValueError, KeyError, TypeError):
                        continue  # poisoned line: read() fail-closes on it anyway
                    if name in carried:
                        continue  # an earlier line for this room is newer: keep it
                    if not store.room_path(root, name).exists():
                        continue  # reaped since the line: carrying it back resurrects it
                    if name in index and entry["mtime"] <= index[name][0]:
                        continue  # older than the snapshot's view: the snapshot wins
                    carry.append(raw)
                    carried.add(name)
            except OSError:
                pass  # nothing readable to carry; the snapshot alone is still complete
            staged = b"\n".join(line.encode() for line in lines)
            if lines:
                staged += b"\n"
            staged += b"".join(raw if raw.endswith(b"\n") else raw + b"\n" for raw in carry)
            store._replace(path, staged)
    except OSError:
        pass


def listing(root: Path, limit: int) -> tuple[list[tuple[float, int, str, int]], int, int]:
    """`(page, total, bytes)` for /rooms: the index when it can answer, else the walk.

    `(entries, total, total_bytes)` where `entries` is recency-descending, at most
    `max(1, min(limit, MAX_LIMIT))` long, each `(mtime, size, name, mtime_ns)`.

    **No path here stats every room.** That is the whole point of the index: at
    production size the walk is ~2275 ms and ~239k syscalls per request. The index path
    costs one file read plus at most `limit` stats — the re-stat exists because the index
    mtime can be stale (an external `utime`, or an appender that recorded before the
    file's own mtime settled) and because `idle_seconds` and the sort order both need the
    real mtime. A stat that fails means the room was reaped since the index last saw it,
    so the entry is dropped and the next newest backfills: a `limit` request returns
    `limit` rooms while any exist, rather than a page short by however many died.

    Totals are the index's own view — `len` and a sum over already-parsed entries, no
    syscalls — and are therefore a **reap-time contract**: a room deleted by the pass now
    running still counts until that pass compacts, which it does immediately after its
    deletes. Measuring them exactly would mean one stat per indexed room, i.e. O(rooms)
    syscalls on every /rooms: the bug this index exists to remove, reinstated by the
    attempt to be exact. The walk below is the exact answer and takes over whenever the
    index cannot be trusted.

    `.usage` is deliberately *not* the source, though it is a maintained figure and would
    be O(1): `_scan` counts every `*.jsonl` under `rooms/`, `p-` capability rooms
    included, and those must never appear in a total. The index is `_listable`-filtered
    by construction.
    """
    limit = max(1, min(int(limit), store.MAX_LIMIT))
    index = read(root)
    if index:
        total = len(index)
        total_bytes = sum(size for _, size in index.values())
        pool = sorted([(mtime, size, name) for name, (mtime, size) in index.items()], reverse=True)
        fresh: list[tuple[float, int, str, int]] = []
        i = 0
        while i < len(pool) and len(fresh) < limit:
            mtime, size, name = pool[i]
            i += 1
            try:
                st = store.room_path(root, name).stat()
            except OSError:
                continue  # reaped between the index write and this stat: backfill behind it
            fresh.append((st.st_mtime, size, name, st.st_mtime_ns))
        # An index that claims a full page and delivers less is stale or dead: the disk is
        # the only authority left, so fall through to the walk rather than serve it.
        if len(fresh) >= limit or len(index) < limit:
            return sorted(fresh, reverse=True), total, total_bytes
    # The walk: O(rooms), the answer whenever the index cannot be trusted, and the one
    # this service gave before the index existed.
    entries: list[tuple[float, int, str, int]] = []
    for e in store._walk(root / "rooms", ".jsonl"):
        name = e.name[: -len(".jsonl")]
        if not store._listable(name):
            continue
        try:
            st = e.stat()
        except OSError:
            continue  # reaped between the readdir and the stat
        entries.append((st.st_mtime, st.st_size, name, st.st_mtime_ns))
    entries.sort(reverse=True)
    return (
        entries[:limit],
        len(entries),
        sum(size for _, size, _, _ in entries),
    )


# ---------------------------------------------------------------------------
# The reaper's side: fold a full pass into the snapshot it publishes
# ---------------------------------------------------------------------------

_SEEN: dict[Path, dict[str, tuple[float, int]]] = {}


def reap_begin(root: Path) -> None:
    """Start a fresh snapshot for this store's next `reap_end()`.

    One pass at a time is guaranteed by `_reap`'s marker lock, so a module-level bucket
    keyed by root is safe here and saves core the line a local dict would cost; it is
    dropped in `reap_end` so a process that reaps a thousand temporary roots keeps none.
    """
    _SEEN[root] = {}


def reap_see(root: Path, entry: os.DirEntry, st: os.stat_result) -> None:
    """Record a room this pass walked and is keeping.

    Called before the pass's reapability checks, for every entry of both trees: `entry`
    is the scandir already gave us, so this decides whether it is a room (and a listable
    one) rather than making core decide — core's own walk is over notes too, and the
    `_listable` filter belongs beside the one that writes the index, not beside the
    counter that totals it.
    """
    if entry.name.endswith(".jsonl") and store._listable(name := _room_of(entry)):
        _SEEN[root][name] = (st.st_mtime, st.st_size)


def reap_unsee(root: Path, entry: os.DirEntry) -> None:
    """Drop a room this pass deleted, so it never reaches the published snapshot."""
    if entry.name.endswith(".jsonl"):
        _SEEN[root].pop(_room_of(entry), None)


def reap_end(root: Path) -> None:
    """Publish this pass's snapshot as the index. Never raises (see `compact`)."""
    index = _SEEN.pop(root, {})
    compact(root, index)


def _room_of(entry: os.DirEntry) -> str:
    return entry.name[: -len(".jsonl")]
