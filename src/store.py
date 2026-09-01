"""Filesystem-backed append-only store for rooms (chat) and notes (KV).

Design constraints (see docs/design.md):
  - one directory tree, no database, no auth
  - rooms are append-only JSONL files, bounded by a sliding window
  - reads never load the whole file: backwards chunked tail only
  - all caller-supplied names pass an allowlist regex, so no path is ever
    built from unvalidated input (traversal impossible by construction)
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import orjson

import config
import didkey

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

MAX_TEXT_CHARS = 4096
MAX_VALUE_CHARS = 8192
MAX_ROOM_BYTES = 10 << 20  # 10 MiB per room, then compacted
COMPACT_KEEP_BYTES, COMPACT_MAX_LINES = MAX_ROOM_BYTES // 2, 5000
READ_BUDGET = 1 << 20  # never read more than 1 MiB to answer a tail request
MAX_LIMIT, DEFAULT_LIMIT = 200, 50

MAX_ROOMS = config.MAX_ROOMS
MAX_TOTAL_ROOM_BYTES = 5 << 30
RESERVED_ROOM_BYTES = MAX_TOTAL_ROOM_BYTES // MAX_ROOMS
USAGE_FILE = ".usage"
NOTES_FILE = ".notes-count"
MAX_NOTES_PER_NS = config.MAX_NOTES_PER_NS
MAX_NOTES_TOTAL = config.MAX_NOTES_TOTAL
EVENTS_ROOM = "events"
EVENTS_NICK = "server"
COUNTERS_FILE = ".counters"
COUNTER_KEYS = (
    "messages",
    "rooms_created",
    "reaped_idle",
    "reaped_stillborn",
    "notes_written",
    "topics_written",
)
SNAPSHOTS_FILE = ".snapshots"
SNAPSHOT_EVERY = 300
SNAPSHOT_KEEP_SECONDS = 30 * 3600
IDLE_SECONDS = 7 * 86400
REAP_EVERY = 300
STILLBORN_SECONDS = 86400
STILLBORN_MESSAGES = 1

ROOM_CLASSES = ("p", "mb", "d", "e")
UNOWNABLE_ROOMS = ("lobby", "meta")
OWNERS_NS = "room-owners"
ALLOW_NS = "room-allow"
NONCE_NS = "room-nonce"
TOPIC_NS = "topic"
TOPIC_PREVIEW_CHARS = 120
EPHEMERAL_TTL_SECONDS = config.EPHEMERAL_TTL_SECONDS


class StoreError(ValueError):
    """Caller-supplied input rejected. Maps to HTTP 400."""


class StoreConflictError(ValueError):
    """A conditional write lost the race. Maps to HTTP 409, and carries the value that
    was actually there so the caller can rebase without a second round trip."""

    def __init__(self, message: str, current: str | None) -> None:
        super().__init__(message)
        self.current = current


def valid_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise StoreError(
            f"bad name {name!r}: expected /{NAME_RE.pattern}/ — lowercase letters, digits, - "
            "and _, 1-48 characters, starting with a letter or digit. Usual causes: uppercase "
            "(lowercase it), a space or %20 (use - instead), a dot or slash, an empty segment, "
            "or over 48 characters. It covers <room>, <nick>, <ns> and <key>; only <text> and "
            "<value> are free-form."
        )
    return name


@lru_cache(maxsize=MAX_ROOMS)
def _listable(name: str) -> bool:
    return NAME_RE.fullmatch(name) is not None and not unlisted(name)


def room_classes(name: str) -> frozenset[str]:
    classes = set()
    for segment in name.split("-")[:-1]:
        if segment not in ROOM_CLASSES:
            break
        classes.add(segment)
    return frozenset(classes)


def unlisted(name: str) -> bool:
    return "p" in room_classes(name)


def is_mailbox(name: str) -> bool:
    return "mb" in room_classes(name)


def is_ephemeral(name: str) -> bool:
    return "e" in room_classes(name)


def ownable(name: str) -> bool:
    return "d" in room_classes(name) and name not in UNOWNABLE_ROOMS


INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")


def clean_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()
    if not text:
        raise StoreError(
            "empty text: nothing visible was left after the single-line sweep, which "
            "replaces every control, format and line-separator character (newline, "
            "zero-width, bidi override, Unicode tag, U+2028) with a space and then trims "
            "the ends. Send at least one visible character."
        )
    if len(text) > limit:
        raise StoreError(
            f"text too long: {len(text)} characters, and the limit is {limit}. Split it, "
            'or send it as a body — POST /r/<room> {"text":...} and POST /kv/<ns>/<key> '
            '{"value":...} carry the full length, which a URL cannot: one CJK character '
            "is 9 bytes URL-encoded and one emoji is 12."
        )
    return text


@lru_cache(maxsize=MAX_ROOMS)
def _shard(name: str, key: bytes | None = None) -> str:
    return hashlib.blake2b(name.encode("utf-8"), digest_size=1, key=key or b"").hexdigest()


def _migrate(legacy: Path, sharded: Path) -> None:
    try:
        sharded.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, sharded)
    except OSError:
        return


def _resolve(d: Path, name: str, suffix: str) -> Path:
    filename = f"{name}{suffix}"
    sharded = d / _shard(name) / filename
    if sharded.exists():
        return sharded
    if (legacy := d / filename).exists():
        _migrate(legacy, sharded)
        if legacy.exists():
            return legacy
    return sharded


def room_path(root: Path, room: str) -> Path:
    return _resolve(root / "rooms", valid_name(room), ".jsonl")


def _note_ns_dir(root: Path, ns: str) -> Path:
    return root / "notes" / valid_name(ns)


def note_path(root: Path, ns: str, key: str) -> Path:
    return _resolve(_note_ns_dir(root, ns), valid_name(key), ".txt")


def _prune(d: Path | str) -> bool:
    empty = True
    try:
        with os.scandir(d) as entries:
            for e in entries:
                if e.is_dir() and _prune(e.path):
                    try:
                        os.rmdir(e.path)
                        continue
                    except OSError:
                        pass
                empty = False
    except OSError:
        return False
    return empty


@contextmanager
def _locked(target: Path, shared: bool = False, nb: bool = False):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(target.suffix + ".lock")
    with open(lock, "a+b") as lf:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if nb:
            flags |= fcntl.LOCK_NB
        fcntl.flock(lf, flags)
        config._dbg(2, "flock", path=target.name)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _replace(path: Path, data: bytes, fsync: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_segments(root: Path) -> list[Path]:
    segments = []
    legacy = root / COUNTERS_FILE
    if legacy.exists():
        segments.append(legacy)
    for p in root.glob(f"{COUNTERS_FILE}.*"):
        ext = p.suffix.lstrip('.')
        if ext.isdigit():
            segments.append(p)
    def get_seq(p: Path) -> int:
        ext = p.suffix.lstrip('.')
        return int(ext) if ext.isdigit() else -1
    segments.sort(key=get_seq)
    return segments


def counters(root: Path) -> dict:
    while True:
        segments = _get_segments(root)
        if not segments:
            return {k: 0 for k in COUNTER_KEYS}
        out = {}
        covered_seq = -2
        success = False
        for seg in reversed(segments):
            seg_seq = int(seg.suffix.lstrip('.')) if seg.suffix.lstrip('.').isdigit() else -1
            if seg_seq <= covered_seq:
                continue
            try:
                with seg.open("rb") as f:
                    seg_lines = f.readlines()
                success = True
            except OSError:
                continue
            for line in seg_lines:
                try:
                    rec = orjson.loads(line)
                    if not isinstance(rec, dict):
                        continue
                    f_seq = rec.pop("_fold_up_to", None)
                    if f_seq is not None and f_seq > covered_seq:
                        covered_seq = f_seq
                    for k in COUNTER_KEYS:
                        v = rec.get(k)
                        if isinstance(v, int):
                            out[k] = out.get(k, 0) + v
                except (ValueError, TypeError, UnicodeDecodeError):
                    pass
        if success or not _get_segments(root):
            return {k: max(0, out.get(k, 0)) for k in COUNTER_KEYS}


def _get_active_segment(root: Path) -> Path:
    segments = _get_segments(root)
    if not segments:
        return root / f"{COUNTERS_FILE}.0"
    return segments[-1]


def _bump(root: Path, **deltas: int) -> None:
    try:
        line = orjson.dumps(deltas) + b"\n"
        active = _get_active_segment(root)
        size = active.stat().st_size if active.exists() else 0
        if size:
            with active.open("rb") as f:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    line = b"\n" + line
        with active.open("ab") as f:
            f.write(line)
    except OSError:
        pass


def _compact_counters(root: Path) -> None:
    try:
        with _locked(root / f"{COUNTERS_FILE}.lock"):
            segments = _get_segments(root)
            if not segments:
                return
            active = segments[-1]
            active_seq = int(active.suffix.lstrip('.')) if active.suffix.lstrip('.').isdigit() else -1
            next_seq = max(0, active_seq + 1)
            next_segment = root / f"{COUNTERS_FILE}.{next_seq}"
            next_segment.touch()
            if len(segments) <= 1:
                return
            fold_target = segments[-2]
            fold_up_to_seq = int(fold_target.suffix.lstrip('.')) if fold_target.suffix.lstrip('.').isdigit() else -1
            fold_segments = [s for s in segments if (int(s.suffix.lstrip('.')) if s.suffix.lstrip('.').isdigit() else -1) <= fold_up_to_seq]
            snap_out = {}
            covered_seq = -2
            for seg in reversed(fold_segments):
                seg_seq = int(seg.suffix.lstrip('.')) if seg.suffix.lstrip('.').isdigit() else -1
                if seg_seq <= covered_seq:
                    continue
                try:
                    with seg.open("rb") as f:
                        lines = f.readlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        rec = orjson.loads(line)
                        if not isinstance(rec, dict):
                            continue
                        f_seq = rec.pop("_fold_up_to", None)
                        if f_seq is not None and f_seq > covered_seq:
                            covered_seq = f_seq
                        for k in COUNTER_KEYS:
                            v = rec.get(k)
                            if isinstance(v, int):
                                snap_out[k] = snap_out.get(k, 0) + v
                    except Exception:
                        pass
            if snap_out:
                snap_out["_fold_up_to"] = fold_up_to_seq
                line = orjson.dumps(snap_out) + b"\n"
                size = next_segment.stat().st_size if next_segment.exists() else 0
                if size:
                    with next_segment.open("rb") as f:
                        f.seek(size - 1)
                        if f.read(1) != b"\n":
                            line = b"\n" + line
                with next_segment.open("ab") as f:
                    f.write(line)
                for seg in fold_segments:
                    try:
                        seg.unlink(missing_ok=True)
                    except OSError:
                        pass
    except OSError:
        pass


def reverse_lines(f, chunk_size: int = 65536, max_bytes: int = READ_BUDGET):
    f.seek(0, os.SEEK_END)
    pos = f.tell()
    head = b""
    read = 0
    while pos > 0 and read < max_bytes:
        step = min(chunk_size, pos, max_bytes - read)
        pos -= step
        f.seek(pos)
        block = f.read(step)
        read += step
        parts = (block + head).split(b"\n")
        head = parts.pop(0)
        for line in reversed(parts):
            if line:
                yield line
    if head and pos == 0:
        yield head


def _cutoff(room: str) -> float | None:
    return time.time() - EPHEMERAL_TTL_SECONDS if is_ephemeral(room) else None


def _expired(rec: dict, cutoff: float) -> bool:
    ts = rec.get("ts")
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=UTC).timestamp() < cutoff
            except ValueError:
                continue
    return True


def _parse(line: bytes) -> dict | None:
    try:
        rec = orjson.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("seq"), int) else None


def read_messages(
    root: Path, room: str, limit: int = DEFAULT_LIMIT, since: int | None = None
) -> dict:
    limit = max(1, min(int(limit), MAX_LIMIT))
    path = room_path(root, room)
    cutoff = _cutoff(room)
    out: list[dict] = []
    if path.exists():
        with path.open("rb") as f:
            for raw in reverse_lines(f):
                rec = _parse(raw)
                if rec is None:
                    continue
                if since is not None and rec["seq"] <= since:
                    break
                if cutoff is not None and _expired(rec, cutoff):
                    break
                out.append(rec)
                if len(out) >= limit:
                    break
    out.reverse()
    return {
        "room": room,
        "count": len(out),
        "first_seq": out[0]["seq"] if out else None,
        "last_seq": out[-1]["seq"] if out else (since or 0),
        "generation": room_generation(root, room),
        "messages": out,
    }


EXPORT_CHUNK = 65536


def _snapshot_bytes(f) -> int:
    pos = os.fstat(f.fileno()).st_size
    while pos > 0:
        step = min(EXPORT_CHUNK, pos)
        f.seek(pos - step)
        if (nl := f.read(step).rfind(b"\n")) != -1:
            return pos - step + nl + 1
        pos -= step
    return 0


def _export_start(f, cutoff: float | None, end: int) -> int:
    if cutoff is None:
        return 0
    f.seek(0)
    pos = 0
    while pos < end:
        line = f.readline()
        rec = _parse(line)
        if rec is not None and not _expired(rec, cutoff):
            return pos
        pos += len(line)
    return end


def export_room(root: Path, room: str) -> tuple[int, Iterator[bytes]]:
    path = room_path(root, room)
    try:
        f = path.open("rb")
    except FileNotFoundError:
        return room_generation(root, room), iter(())
    try:
        end = _snapshot_bytes(f)
        start = _export_start(f, _cutoff(room), end)
        generation = room_generation(root, room)
    except BaseException:
        f.close()
        raise

    def chunks() -> Iterator[bytes]:
        with f:
            f.seek(start)
            remaining = end - start
            while remaining > 0:
                block = f.read(min(EXPORT_CHUNK, remaining))
                if not block:
                    return
                remaining -= len(block)
                yield block

    return generation, chunks()


def _seq_state_path(root: Path, room: str = "") -> Path:
    return root / (f".seqstate.{_shard(room)}" if room else ".seqstate")


def _read_seq_state(path: Path) -> dict:
    try:
        state = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _seq_field(root: Path, room: str, key: str) -> int:
    entry = _read_seq_state(_seq_state_path(root, room)).get(room)
    if not isinstance(entry, dict):
        entry = _read_seq_state(_seq_state_path(root)).get(room)
    value = entry.get(key) if isinstance(entry, dict) else None
    return value if isinstance(value, int) and value >= 0 else 0


def _set_seq_entry(root: Path, room: str, floor: int | None) -> None:
    path = _seq_state_path(root, room)
    try:
        with _locked(path):
            gen = _seq_field(root, room, "gen") + (1 if floor is None else 0)
            state = _read_seq_state(path)
            state[room] = {"floor": floor or 0, "gen": gen, "t": int(time.time())}
            _replace(path, orjson.dumps(state), fsync=config.FSYNC)
    except OSError:
        pass


def last_seq(root: Path, room: str) -> int:
    path = room_path(root, room)
    if path.exists():
        with path.open("rb") as f:
            for raw in reverse_lines(f, max_bytes=65536):
                rec = _parse(raw)
                if rec is not None:
                    return rec["seq"]
        return 0
    return _seq_field(root, room, "floor")


def room_generation(root: Path, room: str) -> int:
    return _seq_field(root, room, "gen")


WINDOW_MESSAGES = 200
WINDOW_BYTES = 65536


def room_window(root: Path, room: str) -> tuple[int, list[str]]:
    nicks: list[str] = []
    top = 0
    path = room_path(root, room)
    if path.exists():
        with path.open("rb") as f:
            for raw in reverse_lines(f, max_bytes=WINDOW_BYTES):
                rec = _parse(raw)
                if rec is None:
                    continue
                if not nicks:
                    top = rec["seq"]
                nicks.append(str(rec.get("from", "")))
                if len(nicks) >= WINDOW_MESSAGES:
                    break
    return top, nicks


def _unanswered(nicks: Sequence[str]) -> int:
    run = 0
    while run < len(nicks) and nicks[run] == nicks[0]:
        run += 1
    return run


def _engagement(nicks: Sequence[str]) -> dict:
    n = len(nicks)
    if not n:
        return {"window": 0, "zero_response_share": None, "nick_diversity": None}
    return {
        "window": n,
        "zero_response_share": round(_unanswered(nicks) / n, 4),
        "nick_diversity": round(len(set(nicks)) / n, 4),
    }


def _rollup(windows: list[Sequence[str]]) -> dict:
    total = sum(len(w) for w in windows)
    if not total:
        return {
            "window_cap": WINDOW_MESSAGES,
            "windowed_messages": 0,
            "zero_response_share": None,
            "nick_diversity": None,
        }
    distinct = len({nick for w in windows for nick in w})
    return {
        "window_cap": WINDOW_MESSAGES,
        "windowed_messages": total,
        "zero_response_share": round(sum(_unanswered(w) for w in windows) / total, 4),
        "nick_diversity": round(distinct / total, 4),
    }


def list_rooms(root: Path) -> list[str]:
    names = (e.name[: -len(".jsonl")] for e in _walk(root / "rooms", ".jsonl"))
    return sorted(n for n in names if _listable(n))


def _time_bucket(now: float, ttl: float) -> int:
    return int(now // ttl)


_WINDOW_MEMO_MAX = 512


@lru_cache(maxsize=_WINDOW_MEMO_MAX)
def _cached_window(root: str, name: str, stamp: tuple) -> tuple[int, tuple[str, ...]]:
    top, nicks = room_window(Path(root), name)
    return top, tuple(nicks)


_TOPICS_MEMO_MAX = 512


@lru_cache(maxsize=_TOPICS_MEMO_MAX)
def _topics_memo(root: str, room: str, stamp: tuple, bucket: int) -> str | None:
    return topic(Path(root), room)


def _cached_topic(root: str, room: str, stamp: tuple, now: float) -> str | None:
    ttl = config.NOTE_STATS_CACHE_SECONDS
    if ttl <= 0:
        return topic(Path(root), room)
    return _topics_memo(root, room, stamp, _time_bucket(now, ttl))


def room_stats(root: Path, limit: int = DEFAULT_LIMIT) -> dict:
    now = time.time()
    entries = []
    for e in _walk(root / "rooms", ".jsonl"):
        name = e.name[: -len(".jsonl")]
        if not _listable(name):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, name, st.st_mtime_ns))
    entries.sort(reverse=True)
    shown = []
    windows = []
    root_key = str(root)
    topics_stamp = (counters(root)["topics_written"], root_key)
    mono = time.monotonic()
    for mtime, size, name, mtime_ns in entries[: max(1, min(int(limit), MAX_LIMIT))]:
        top, nicks = _cached_window(root_key, name, (mtime_ns, size))
        windows.append(nicks)
        shown.append(
            {
                "room": name,
                "last_seq": top,
                "bytes": size,
                "idle_seconds": max(0, int(now - mtime)),
                "topic": _cached_topic(root_key, name, topics_stamp, mono),
                **_engagement(nicks),
            }
        )
    return {
        "rooms": shown,
        "total": len(entries),
        "capacity": MAX_ROOMS,
        "bytes": sum(e[1] for e in entries),
        "bytes_capacity": MAX_TOTAL_ROOM_BYTES,
        "engagement": _rollup(windows),
    }


def service_stats(root: Path, engagement_rooms: int = 50) -> dict:
    keys = ("total", "listed", "unlisted", "open", "mailbox", "ownable", "ephemeral")
    rooms = dict.fromkeys(keys, 0)
    room_bytes = 0
    for e in _walk(root / "rooms", ".jsonl"):
        name = e.name[: -len(".jsonl")]
        if not NAME_RE.fullmatch(name):
            continue
        try:
            room_bytes += e.stat().st_size
        except OSError:
            continue
        classes = room_classes(name)
        rooms["total"] += 1
        rooms["unlisted" if "p" in classes else "listed"] += 1
        for marker, key in (("mb", "mailbox"), ("d", "ownable"), ("e", "ephemeral")):
            if marker in classes:
                rooms[key] += 1
        if not classes:
            rooms["open"] += 1
    notes = note_stats(root)
    return {
        "rooms": {**rooms, "capacity": MAX_ROOMS},
        "bytes": {
            "rooms": room_bytes,
            "notes": notes["bytes"],
            "rooms_capacity": MAX_TOTAL_ROOM_BYTES,
        },
        "notes": notes,
        "counters": counters(root),
        "engagement": room_stats(root, limit=engagement_rooms)["engagement"],
    }


def _stillborn(path: Path | str) -> bool:
    seen = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                if _parse(line) is None:
                    continue
                seen += 1
                if seen > STILLBORN_MESSAGES:
                    return False
    except OSError:
        return False
    return True


def _reapable(path: Path | str, now: float, stillborn_rule: bool) -> str | None:
    idle = now - os.stat(path).st_mtime
    if idle > IDLE_SECONDS:
        return "idle"
    if stillborn_rule and idle > STILLBORN_SECONDS and _stillborn(path):
        return "stillborn"
    return None


ROOM_GUARD_NS = (OWNERS_NS, ALLOW_NS, NONCE_NS)


def _guards_a_live_room(root: Path, base: str, entry: os.DirEntry[str], now: float) -> bool:
    if entry.path[len(base) :].partition(os.sep)[0] not in ROOM_GUARD_NS:
        return False
    room = room_path(root, entry.name.rpartition(".")[0])
    try:
        return now - room.stat().st_mtime <= IDLE_SECONDS
    except OSError:
        return False


def _reconcile_note_count(root: Path) -> None:
    try:
        with _locked((root / NOTES_FILE).with_suffix(".create")):
            _write_note_count(root, *_count_notes(root))
    except OSError:
        pass


def _split_seq_state(root: Path) -> None:
    legacy = _seq_state_path(root)
    shards: dict[Path, dict] = {}
    try:
        with _locked(legacy):
            first = not (backup := legacy.with_suffix(".pre-shard")).exists()
            for room, entry in _read_seq_state(legacy).items():
                shards.setdefault(_seq_state_path(root, room), {})[room] = entry
            for path, entries in shards.items():
                with _locked(path):
                    shard = _read_seq_state(path)
                    merged = {**entries, **shard} if first else {**shard, **entries}
                    _replace(path, orjson.dumps(merged), fsync=config.FSYNC)
            legacy.replace(backup) if first else legacy.unlink()
    except OSError:
        pass


def _sweep_orphan_locks(root: Path, now: float) -> None:
    for sub, suffix in (("rooms", ".jsonl.lock"), ("notes", ".txt.lock")):
        for entry in _walk(root / sub, suffix):
            try:
                data = entry.path[: -len(".lock")]
                if os.access(data, os.F_OK) or now - entry.stat().st_mtime <= IDLE_SECONDS:
                    continue
                os.unlink(entry.path)
            except OSError:
                continue


def _drop_emptied_namespaces(root: Path) -> None:
    for d in (root / "notes").glob("*"):
        try:
            with _locked((root / NOTES_FILE).with_suffix(".create")):
                (d / NOTES_FILE).unlink(missing_ok=True)
                _prune(d)
                d.rmdir()
        except OSError:
            continue


def _reap(root: Path) -> None:
    marker = root / ".reaped"
    now = time.time()
    try:
        if now - marker.stat().st_mtime < REAP_EVERY:
            return
    except FileNotFoundError:
        pass
    root.mkdir(parents=True, exist_ok=True)
    marker.touch()
    reaped = {"reaped_idle": 0, "reaped_stillborn": 0}
    for sub, suffix, stillborn_rule in (("rooms", ".jsonl", True), ("notes", ".txt", False)):
        base = f"{root / sub}{os.sep}"
        for entry in _walk(root / sub, suffix):
            try:
                if _guards_a_live_room(root, base, entry, now):
                    continue
                if not _reapable(entry.path, now, stillborn_rule):
                    continue
                p = Path(entry.path)
                with _locked(p):
                    reason = _reapable(p, now, stillborn_rule)
                    if reason:
                        if stillborn_rule:
                            room = p.name[: -len(".jsonl")]
                            _set_seq_entry(root, room, max(0, last_seq(root, room)))
                        p.unlink(missing_ok=True)
                        config._dbg(2, "reap", room=p.name, reason=reason)
                        if stillborn_rule:
                            reaped[f"reaped_{reason}"] += 1
            except OSError:
                continue
    if any(reaped.values()):
        _bump(root, **reaped)
    _compact_counters(root)
    _reconcile_note_count(root)
    _sweep_orphan_locks(root, now)
    _drop_emptied_namespaces(root)
    _split_seq_state(root)
    try:
        with _locked((root / USAGE_FILE).with_suffix(".create")):
            _write_note_count(root, *_count_rooms(root), name=USAGE_FILE)
            _prune(root / "rooms")
    except OSError:
        pass


def snapshots(root: Path) -> list[dict]:
    out = []
    try:
        lines = (root / SNAPSHOTS_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return out
    for line in lines:
        try:
            rec = orjson.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("t"), (int, float)):
            out.append(rec)
    out.sort(key=lambda r: r["t"])
    return out


def _snapshot(root: Path) -> None:
    marker = root / SNAPSHOTS_FILE
    now = time.time()
    try:
        if now - marker.stat().st_mtime < SNAPSHOT_EVERY:
            return
    except FileNotFoundError:
        pass
    except OSError:
        return
    try:
        with _locked(marker):
            try:
                if time.time() - marker.stat().st_mtime < SNAPSHOT_EVERY:
                    return
            except FileNotFoundError:
                pass
            kept = [r for r in snapshots(root) if now - r["t"] <= SNAPSHOT_KEEP_SECONDS]
            kept.append({"t": int(now), **service_stats(root)})
            _replace(marker, b"".join(orjson.dumps(r) + b"\n" for r in kept))
    except OSError:
        pass


def _scan(d: Path | str, suffix: str, sized: bool = False) -> tuple[int, int]:
    count = 0
    size = 0
    try:
        with os.scandir(d) as entries:
            for e in entries:
                if e.is_dir():
                    sub_count, sub_size = _scan(e.path, suffix, sized)
                    count += sub_count
                    size += sub_size
                elif e.name.endswith(suffix):
                    count += 1
                    if sized:
                        try:
                            size += e.stat().st_size
                        except OSError:
                            continue
    except OSError:
        pass
    return count, size


def _walk(d: Path | str, suffix: str) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(d) as entries:
            for e in entries:
                if e.is_dir():
                    yield from _walk(e.path, suffix)
                elif e.name.endswith(suffix):
                    yield e
    except OSError:
        return


def _count_notes(root: Path) -> tuple[int, int]:
    total = 0
    size = 0
    try:
        with os.scandir(root / "notes") as namespaces:
            for ns in namespaces:
                if ns.is_dir():
                    count, ns_bytes = _scan(ns.path, ".txt", sized=True)
                    total += count
                    size += ns_bytes
    except FileNotFoundError:
        pass
    return total, size


def _write_note_count(root: Path, total: int, size: int, name: str = NOTES_FILE) -> None:
    _replace(root / name, f"{total} {size}".encode())


def _ns_totals(d: Path) -> tuple[int, int]:
    return _scan(d, ".txt", sized=True)


def _note_totals(d: Path, rebuild=_count_notes, persist=False, name=NOTES_FILE) -> tuple[int, int]:
    try:
        count, size = (d / name).read_text(encoding="utf-8").split()
        if int(count) >= 0 and int(size) >= 0:
            return int(count), int(size)
    except (OSError, ValueError):
        pass
    totals = rebuild(d)
    if persist and totals[0]:
        try:
            _write_note_count(d, *totals, name=name)
        except OSError:
            pass
    return totals


def _note_count(root: Path) -> int:
    return _note_totals(root)[0]


def _count_new_note(root: Path, ns_dir: Path, size: int, delta: int) -> None:
    count, used = _note_totals(root)
    _write_note_count(root, max(0, count + delta), max(0, used + size * delta))
    ns_count, ns_used = _note_totals(ns_dir, _ns_totals)
    _write_note_count(ns_dir, max(0, ns_count + delta), max(0, ns_used + size * delta))


def _count_rooms(root: Path) -> tuple[int, int]:
    return _scan(root / "rooms", ".jsonl", sized=True)


def _count_new_room(root: Path, delta: int) -> None:
    count, used = _note_totals(root, _count_rooms, name=USAGE_FILE)
    _write_note_count(root, max(0, count + delta), used, name=USAGE_FILE)


def _at_capacity(cap: int, what: str) -> StoreError:
    return StoreError(
        f"{what} limit reached ({cap} is the cap, and this would be a new one). "
        f"Existing {what}s still accept writes, so reuse one you already have — "
        f"GET /rooms shows what exists. Idle {what}s are reclaimed after 7 days "
        "(a room still on its first message goes after 24 hours)."
    )


def room_bytes_used(root: Path) -> int:
    try:
        return int((root / USAGE_FILE).read_text(encoding="utf-8").split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _ring_limit(root: Path) -> int:
    if room_bytes_used(root) < MAX_TOTAL_ROOM_BYTES:
        return MAX_ROOM_BYTES
    return RESERVED_ROOM_BYTES


def _check_room_capacity(root: Path, path: Path) -> None:
    if path.exists():
        return
    count, used = _note_totals(root, _count_rooms, name=USAGE_FILE)
    if count >= MAX_ROOMS:
        raise _at_capacity(MAX_ROOMS, "room")
    if used >= MAX_TOTAL_ROOM_BYTES:
        raise StoreError(
            f"room storage is full ({used >> 20} MiB of a {MAX_TOTAL_ROOM_BYTES >> 20} MiB "
            "budget, and this would be a new room). The cap is on total bytes, not on the "
            "number of rooms, so a shorter name buys nothing. Existing rooms still accept "
            "writes, so reuse one you already have — GET /rooms shows what exists. Idle "
            "rooms are reclaimed after 7 days (a room still on its first message goes "
            "after 24 hours)."
        )


def _check_note_capacity(root: Path, ns_dir: Path, path: Path) -> None:
    if path.exists():
        return
    if _note_totals(ns_dir, _ns_totals, persist=True)[0] >= MAX_NOTES_PER_NS:
        raise _at_capacity(MAX_NOTES_PER_NS, "note")
    if _note_count(root) >= MAX_NOTES_TOTAL:
        raise StoreError(
            f"note limit reached ({MAX_NOTES_TOTAL} across all namespaces, and this would "
            "be a new one). A fresh namespace buys nothing — the cap is global. Overwrite "
            "a note you already own instead; idle notes are reclaimed after 7 days, and "
            "GET /rooms reports how full the note store is."
        )


@contextmanager
def _create_gate(gate: Path, path: Path, check, counted):
    if path.exists():
        with _locked(path):
            yield
        return
    check()
    with _locked(gate.with_suffix(".create"), shared=True), _locked(path):
        reserved = False
        if not path.exists():
            with _locked(gate):
                check()
                counted(1)
                reserved = True
        try:
            yield
        finally:
            if reserved and not path.exists():
                with _locked(gate):
                    counted(-1)


def append(
    root: Path,
    room: str,
    nick: str,
    text: str,
    did: str | None = None,
    nonce: int | None = None,
    sig: str | None = None,
) -> dict:
    rec, created = _write_record(root, room, nick, text, did=did, nonce=nonce, sig=sig)
    _bump(root, messages=1, **({"rooms_created": 1} if created else {}))
    if created and room != EVENTS_ROOM and not unlisted(room):
        _log_event(root, f"created {room}")
    _snapshot(root)
    return rec


def _log_event(root: Path, line: str) -> None:
    try:
        _write_record(root, EVENTS_ROOM, EVENTS_NICK, line)
    except Exception:
        pass


def _last_nonce(root: Path, room: str, did: str) -> int | None:
    path = room_path(root, room)
    if not path.exists():
        return None
    did_b = did.encode()
    with path.open("rb") as f:
        for raw in reverse_lines(f):
            if did_b not in raw:
                continue
            rec = _parse(raw)
            if rec is not None and rec.get("from") == did and isinstance(rec.get("nonce"), int):
                return rec["nonce"]
    return None


def _write_record(
    root: Path,
    room: str,
    nick: str,
    text: str,
    did: str | None = None,
    nonce: int | None = None,
    sig: str | None = None,
) -> tuple[dict, bool]:
    path = room_path(root, room)
    if did is None:
        rec = {"seq": 0, "ts": _now(), "from": valid_name(nick), "text": clean_text(text)}
    else:
        didkey.public_key(did)
        if not isinstance(nonce, int) or nonce < 0:
            raise StoreError(
                f"signed writes need a non-negative integer nonce, got {nonce!r} — 1-19 "
                "digits, greater than the last one this key used in this room. A counter "
                "or a millisecond clock both work"
            )
        rec = {"seq": 0, "ts": _now(), "from": did, "text": clean_text(text), "nonce": nonce}
        if sig is not None:
            rec["sig"] = sig
    _reap(root)
    with _create_gate(
        root / USAGE_FILE,
        path,
        lambda: _check_room_capacity(root, path),
        lambda d: _count_new_room(root, d),
    ):
        created = not path.exists()
        if did is not None:
            if nonce is None:
                raise StoreError(
                    "a signed write must carry a nonce: it is what makes a captured "
                    "signed URL single-use. Send 1-19 digits, counting up per key per room"
                )
            previous = _last_nonce(root, room, did)
            if previous is not None and nonce <= previous:
                raise StoreError(
                    f"nonce {nonce} is not greater than {previous}, the last one this key "
                    f"used in /r/{room} — a signed URL is single-use, so count up"
                )
        rec["seq"] = last_seq(root, room) + 1
        line = orjson.dumps(rec) + b"\n"
        size = path.stat().st_size if path.exists() else 0
        if size:
            with path.open("rb") as f:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    line = b"\n" + line
        with path.open("ab") as f:
            f.write(line)
            f.flush()
            if config.FSYNC:
                os.fsync(f.fileno())
        limit = _ring_limit(root)
        if path.stat().st_size > limit:
            _compact(path, cutoff=_cutoff(room), keep=limit // 2)
    if created:
        _set_seq_entry(root, room, None)
    return rec, created


def _compact(path: Path, cutoff: float | None = None, keep: int = COMPACT_KEEP_BYTES) -> None:
    kept: list[bytes] = []
    total = 0
    with path.open("rb") as f:
        for line in reverse_lines(f, max_bytes=MAX_ROOM_BYTES):
            total += len(line) + 1
            if total > keep or len(kept) >= COMPACT_MAX_LINES:
                break
            if cutoff is not None and kept:
                rec = _parse(line)
                if rec is None or _expired(rec, cutoff):
                    break
            kept.append(line)
    kept.reverse()
    _replace(path, b"".join(line + b"\n" for line in kept), fsync=True)
    config._dbg(2, "compact", room=path.name, kept=len(kept), bytes=total)


def note_set(
    root: Path,
    ns: str,
    key: str,
    value: str,
    expect: str | None = None,
    expect_absent: bool = False,
) -> dict:
    path = note_path(root, ns, key)
    ns_dir = _note_ns_dir(root, ns)
    value = clean_text(value, MAX_VALUE_CHARS)
    _reap(root)
    with _create_gate(
        root / NOTES_FILE,
        path,
        lambda: _check_note_capacity(root, ns_dir, path),
        lambda d: _count_new_note(root, ns_dir, len(value.encode("utf-8")), d),
    ):
        if expect_absent or expect is not None:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if expect_absent and current is not None:
                config._dbg(2, "cas_conflict", ns=ns, key=key, found="exists")
                raise StoreConflictError(f"note {ns}/{key} already exists", current)
            if expect is not None and current != expect:
                config._dbg(2, "cas_conflict", ns=ns, key=key, found="changed")
                raise StoreConflictError(f"note {ns}/{key} changed since you read it", current)
        _replace(path, value.encode("utf-8"))
    _bump(root, notes_written=1, **({"topics_written": 1} if ns == TOPIC_NS else {}))
    return {"ns": ns, "key": key, "bytes": len(value.encode()), "ts": _now()}


def note_get(root: Path, ns: str, key: str) -> str | None:
    path = note_path(root, ns, key)
    return path.read_text(encoding="utf-8") if path.exists() else None


def topic(root: Path, room: str) -> str | None:
    value = note_get(root, TOPIC_NS, room)
    if value is None:
        return None
    return value if len(value) <= TOPIC_PREVIEW_CHARS else value[:TOPIC_PREVIEW_CHARS] + "…"


def note_stats(root: Path) -> dict:
    total, size = _note_totals(root)
    caps = {"capacity": MAX_NOTES_TOTAL, "capacity_per_namespace": MAX_NOTES_PER_NS}
    return {"total": total, "bytes": size, **caps}


def list_notes(root: Path, ns: str) -> list[str]:
    keep = _listable.__wrapped__
    names = (e.name[: -len(".txt")] for e in _walk(_note_ns_dir(root, ns), ".txt"))
    return sorted(n for n in names if keep(n))