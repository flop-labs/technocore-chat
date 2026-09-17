"""Seek helpers for byte-exact room exports."""

from __future__ import annotations

from collections.abc import Callable

Parse = Callable[[bytes], dict | None]


def _line_at_or_after(f, pos: int, end: int) -> tuple[int, bytes]:
    if pos <= 0:
        f.seek(0)
    else:
        f.seek(pos - 1)
        if f.read(1) != b"\n":
            f.readline()
    start = f.tell()
    if start >= end:
        return end, b""
    return start, f.readline()


def after_start(f, end: int, after: int, parse: Parse) -> int | None:
    """Return the byte offset for the first monotonic JSONL record after `after`.

    `None` means the caller should fall back to its conservative forward scan because the
    file is malformed enough that seq ordering is not safe to infer.
    """
    lo, hi = 0, end
    for _ in range(max(1, end.bit_length() + 1)):
        if lo >= hi:
            break
        mid = (lo + hi) // 2
        start, line = _line_at_or_after(f, mid, end)
        if not line:
            hi = mid
            continue
        rec = parse(line)
        seq = rec.get("seq") if rec is not None else None
        if not isinstance(seq, int):
            return None
        if seq <= after:
            new_lo = f.tell()
            if new_lo <= lo:
                break
            lo = new_lo
        else:
            new_hi = mid if start >= hi else start
            if new_hi >= hi:
                break
            hi = new_hi
    start, line = _line_at_or_after(f, lo, end)
    while line:
        rec = parse(line)
        seq = rec.get("seq") if rec is not None else None
        if isinstance(seq, int) and seq > after:
            return start
        start = f.tell()
        line = f.readline()
    return end
