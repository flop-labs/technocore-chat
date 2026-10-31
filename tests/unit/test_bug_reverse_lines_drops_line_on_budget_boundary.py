"""Regression test: reverse_lines() must yield a record that lies entirely inside its
`max_bytes` budget, including the one whose last byte IS the budget's last byte.

`reverse_lines` (src/store.py) reads backwards in chunks and keeps the possibly-incomplete
first line of the block in `head`. `head` was only ever yielded when the scan reached
offset 0, so when the budget stopped the scan exactly on a record boundary -- the byte in
front of `head` being the newline that ended the older record -- a whole record whose bytes
had just been read was silently discarded.

Observable through `read_messages`, whose budget is READ_BUDGET: the tail read omits a
message that is still on disk and reports a `first_seq` one higher, which the published
room-view schema defines as "the ring dropped messages you never read".

The helper has three more callers with their own budgets. `_last_nonce` and `room_window`
are pinned below at the same boundary, because the fix is in the shared helper and a later
change to one call site must not quietly take it away from the others.
"""

import io

from _client import _keypair

import store


def _exact_tail_budget(path, keep: int) -> int:
    """A budget whose far edge is exactly the front edge of the `keep`-th newest record.

    The backwards scan then holds that whole record in `head`, with the newline that ended
    the record before it sitting one byte further back. That is the case this file is about.
    """
    lines = path.read_bytes().splitlines(keepends=True)
    assert len(lines) > keep, (
        "older records have to sit in front of the budget, or the scan reaches offset 0 and "
        "the pre-fix code yields `head` for that reason instead of the one under test"
    )
    return sum(len(line) for line in lines[-keep:])


def test_reverse_lines_yields_a_record_that_ends_on_the_budget_boundary() -> None:
    data = b"aaa\nbbb\n"  # two complete records, four bytes each
    # Four bytes is all of b"bbb\n" and none of the older record.
    assert list(store.reverse_lines(io.BytesIO(data), chunk_size=4, max_bytes=4)) == [b"bbb"]
    # One byte short of it is still a partial record, and a partial record is not a record.
    assert list(store.reverse_lines(io.BytesIO(data), chunk_size=4, max_bytes=3)) == []
    # Unchanged either side of the boundary.
    assert list(store.reverse_lines(io.BytesIO(data), chunk_size=4, max_bytes=5)) == [b"bbb"]
    assert list(store.reverse_lines(io.BytesIO(data), chunk_size=4, max_bytes=8)) == [
        b"bbb",
        b"aaa",
    ]


def test_a_tail_read_keeps_the_oldest_record_inside_the_read_budget(tmp_path, monkeypatch) -> None:
    for i in range(3):
        store.append(tmp_path, "lobby", "bot", f"message {i}")
    lines = store.room_path(tmp_path, "lobby").read_bytes().splitlines(keepends=True)
    # A budget that is exactly the newest two records, with older history in front of them
    # so the backwards scan never reaches offset 0. The budget is a default argument, which
    # is why it is patched there rather than on the module (see test_signed_lane_stateful).
    budget = sum(len(line) for line in lines[-2:])
    defaults = store.reverse_lines.__defaults__
    assert defaults is not None
    chunk_size, _ = defaults
    monkeypatch.setattr(store.reverse_lines, "__defaults__", (chunk_size, budget))

    view = store.read_messages(tmp_path, "lobby", limit=store.MAX_LIMIT)

    assert [m["seq"] for m in view["messages"]] == [2, 3], (
        "both records lie entirely inside the read budget, so both must come back"
    )
    assert view["first_seq"] == 2, "first_seq must not claim a gap the ring did not make"


def test_last_nonce_guards_a_record_that_ends_on_the_read_budget(tmp_path, monkeypatch) -> None:
    """The helper's other default-budget caller, at the same boundary.

    `_last_nonce` decides whether a captured signed URL is a replay, so the newest record it
    can see for a key is the record it still refuses. The boundary cost it one record whose
    bytes it had already read and charged.

    This is not a replay of a message a reader can still see. `read_messages` shares this
    budget, so before the fix both dropped the same record in the same breath and the
    coupling tests/test_signed_lane_stateful.py pins held either way. Measured on 73a7324 at
    the budget below, the reader returned seq 4 alone while this returned None. The fix moves
    both together, so the guarded window grows by that record and still matches the readable
    one.
    """
    did, _ = _keypair(3)
    store.append(tmp_path, "lobby", "filler", "older history one")
    store.append(tmp_path, "lobby", "filler", "older history two")
    store.append(tmp_path, "lobby", "", "guarded", did=did, nonce=4242)
    store.append(tmp_path, "lobby", "filler", "newer traffic")
    path = store.room_path(tmp_path, "lobby")
    lines = path.read_bytes().splitlines(keepends=True)
    assert did.encode() in lines[-2] and did.encode() not in lines[-1], (
        "the signed record must be the oldest line inside the budget and the only line in "
        "there carrying the DID, or this measures some other record"
    )
    budget = _exact_tail_budget(path, 2)
    defaults = store.reverse_lines.__defaults__
    assert defaults is not None
    chunk_size, _ = defaults

    def at(size: int) -> int | None:
        # A default argument, so patched there rather than on the module. Same reason as the
        # read_messages test above and `_window` in tests/test_signed_lane_stateful.py.
        monkeypatch.setattr(store.reverse_lines, "__defaults__", (chunk_size, size))
        return store._last_nonce(tmp_path, "lobby", did)

    assert at(budget) == 4242, "the record lies entirely inside the budget, so it must guard"
    # Both neighbours answer the same before and after the fix, which is what makes this a
    # boundary and not a widened window: one byte less genuinely cuts the record, one byte
    # more uncovers its terminator so the ordinary path yields it.
    assert at(budget - 1) is None
    assert at(budget + 1) == 4242


def test_room_window_counts_a_record_that_ends_on_the_window_budget(tmp_path, monkeypatch) -> None:
    """The caller that passes its own budget instead of taking the default.

    `last_seq` and the overview aggregates come out of this single scan, so a record dropped
    here is a nick missing from the window and an `_unanswered` run measured over the wrong
    set.

    The budget is patched rather than filled to a real WINDOW_BYTES. Nothing in the bug
    depends on the size of the window, only on a record edge landing on its far end, and a
    fixture padded to 65536 bytes would pin the byte width of the JSON encoder as well.
    """
    for i in range(6):
        store.append(tmp_path, "lobby", f"nick{i}", f"message {i}")
    budget = _exact_tail_budget(store.room_path(tmp_path, "lobby"), 4)

    def at(size: int) -> tuple[int, list[str]]:
        monkeypatch.setattr(store, "WINDOW_BYTES", size)
        return store.room_window(tmp_path, "lobby")

    assert at(budget) == (6, ["nick5", "nick4", "nick3", "nick2"])
    # `nick2` is the record the boundary decides. Its neighbours are what the fix leaves alone.
    assert at(budget - 1) == (6, ["nick5", "nick4", "nick3"])
    assert at(budget + 1) == (6, ["nick5", "nick4", "nick3", "nick2"])
