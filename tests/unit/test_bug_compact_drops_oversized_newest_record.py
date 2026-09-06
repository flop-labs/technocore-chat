"""Regression test: _compact() must never drop the newest record, even when that
record alone is larger than the byte budget it is compacting against.

`_compact` (src/store.py) documents — and is separately guarded by
`test_the_byte_budget_bounds_growth_and_not_only_creation` and by the Hypothesis
state machine's `last_seq_never_goes_backwards` rule/docstring — that it always
retains the newest record, "because `seq` is read back from it. Compacting a
room to nothing would restart the sequence at 1 and silently strand every
cursor pointing past it."

That guarantee was only implemented for the `cutoff` (ephemeral-expiry) path:
the plain byte-budget check (`total > keep`) ran unconditionally, including on
the very first (newest) line, so a single record bigger than `keep` compacted
the room to an empty file and reset last_seq to 0 -- even though append()
had just returned that record's seq as a successful write.

Reachable in production whenever a room's per-write compaction budget
(`_ring_limit(root) // 2`) is smaller than a single record's on-disk size --
e.g. an operator-raised CHAT_MAX_ROOMS shrinking RESERVED_ROOM_BYTES below
~16.5 KB (a legal max-length 4-byte-UTF-8 message) while the store is over
its total room-byte budget.
"""

import store


def test_compaction_never_drops_the_newest_record_even_when_it_alone_exceeds_keep(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "MAX_TOTAL_ROOM_BYTES", 10_000)
    monkeypatch.setattr(store, "RESERVED_ROOM_BYTES", 100)
    monkeypatch.setattr(store, "room_bytes_used", lambda _root: store.MAX_TOTAL_ROOM_BYTES + 1)

    store.append(tmp_path, "lobby", "bot", "seed")
    assert store._ring_limit(tmp_path) == store.RESERVED_ROOM_BYTES

    rec = store.append(tmp_path, "lobby", "bot", "x" * 300)

    path = store.room_path(tmp_path, "lobby")
    assert path.stat().st_size > 0, "compaction dropped every record, including the newest"
    assert store.last_seq(tmp_path, "lobby") == rec["seq"], (
        "last_seq must never go backwards / reset after a successful append"
    )
    view = store.read_messages(tmp_path, "lobby", limit=10)
    assert view["messages"], "the just-written message vanished after compaction"
    assert view["messages"][-1]["seq"] == rec["seq"]
