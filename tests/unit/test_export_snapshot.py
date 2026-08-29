"""store.export_room: the snapshot bound, and what may move underneath it.

The interleavings the HTTP layer cannot stage — an append or a compaction landing between
two chunks of one export — are staged here by driving the iterator by hand.
"""

import pytest

import store
from store import StoreError


def _fill(root, room, n=3):
    for i in range(n):
        store.append(root, room, "bot", f"line {i} " + "x" * 40)
    return store.room_path(root, room)


def test_an_append_mid_export_lands_outside_the_snapshot(tmp_path, monkeypatch):
    """The bound is the size at open, so a stream in flight never picks up bytes that
    arrived after it — bounded output, and no torn record however the writes race."""
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)  # force many chunks from a small room
    path = _fill(tmp_path, "racing")
    snapshot = path.read_bytes()

    chunks = store.export_room(tmp_path, "racing")
    first = next(chunks)
    store.append(tmp_path, "racing", "bot", "landed after the export opened")
    assert first + b"".join(chunks) == snapshot
    assert path.read_bytes() != snapshot  # the append really did move the file


def test_a_compaction_mid_export_keeps_reading_the_opened_inode(tmp_path, monkeypatch):
    """Compaction goes through `_replace`, an atomic os.replace — the exporter's fd keeps
    the old inode alive, so no lock is held across the stream and none is needed."""
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)
    path = _fill(tmp_path, "compacted")
    snapshot = path.read_bytes()

    chunks = store.export_room(tmp_path, "compacted")
    first = next(chunks)
    store._replace(path, b'{"seq": 99}\n')
    assert first + b"".join(chunks) == snapshot


def test_the_snapshot_stops_at_the_last_complete_line(tmp_path):
    path = _fill(tmp_path, "torn")
    whole = path.read_bytes()
    with path.open("ab") as f:
        f.write(b'{"seq": 4, "ts": "20')  # a write cut short: no newline, not a record
    assert b"".join(store.export_room(tmp_path, "torn")) == whole


def test_an_absent_room_exports_nothing_and_creates_nothing(tmp_path):
    assert b"".join(store.export_room(tmp_path, "ghost")) == b""
    assert not (tmp_path / "rooms").exists()


def test_a_bad_name_refuses_before_the_stream_starts(tmp_path):
    """valid_name runs at the call, not at the first chunk: a refusal must become a 400,
    which is only possible while no bytes of a 200 have been promised yet."""
    with pytest.raises(StoreError):
        store.export_room(tmp_path, "NOT-A-NAME")
