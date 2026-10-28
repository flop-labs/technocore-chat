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
"""

import io

import store


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
