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


def _export(root, room):
    generation, chunks = store.export_room(root, room)
    return b"".join(chunks)


def test_an_append_mid_export_lands_outside_the_snapshot(tmp_path, monkeypatch):
    """The bound is the size at open, so a stream in flight never picks up bytes that
    arrived after it — bounded output, and no torn record however the writes race."""
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)  # force many chunks from a small room
    path = _fill(tmp_path, "racing")
    snapshot = path.read_bytes()

    _, chunks = store.export_room(tmp_path, "racing")
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

    _, chunks = store.export_room(tmp_path, "compacted")
    first = next(chunks)
    store._replace(path, b'{"seq": 99}\n')
    assert first + b"".join(chunks) == snapshot


def test_the_snapshot_stops_at_the_last_complete_line(tmp_path):
    path = _fill(tmp_path, "torn")
    whole = path.read_bytes()
    with path.open("ab") as f:
        f.write(b'{"seq": 4, "ts": "20')  # a write cut short: no newline, not a record
    assert _export(tmp_path, "torn") == whole


def test_an_ephemeral_room_exports_only_what_is_still_readable(tmp_path, monkeypatch):
    """Expiry is drop-on-read and export is a read: records the `e-` class promises have
    stopped being readable must not come back out through the raw lane (PR #505 review).
    The unexpired suffix still leaves byte-for-byte."""
    from datetime import UTC, datetime, timedelta

    stale = datetime.now(UTC) - timedelta(seconds=store.EPHEMERAL_TTL_SECONDS + 60)
    monkeypatch.setattr(store, "_now", lambda: stale.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    for i in range(2):
        store.append(tmp_path, "e-decay", "bot", f"expired {i}")
    monkeypatch.undo()
    store.append(tmp_path, "e-decay", "bot", "still here")

    exported = _export(tmp_path, "e-decay")
    live = [
        line
        for line in store.room_path(tmp_path, "e-decay").read_bytes().splitlines()
        if b"still here" in line
    ]
    assert exported == live[0] + b"\n"
    # The prefix scan is `e-` only: a plain room with old timestamps exports whole.
    monkeypatch.setattr(store, "_now", lambda: stale.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    store.append(tmp_path, "keeps", "bot", "old but durable")
    monkeypatch.undo()
    assert _export(tmp_path, "keeps") == store.room_path(tmp_path, "keeps").read_bytes()


def test_a_fully_expired_ephemeral_room_exports_nothing(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    stale = datetime.now(UTC) - timedelta(seconds=store.EPHEMERAL_TTL_SECONDS + 60)
    monkeypatch.setattr(store, "_now", lambda: stale.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    store.append(tmp_path, "e-gone", "bot", "already past the ttl")
    monkeypatch.undo()
    assert _export(tmp_path, "e-gone") == b""


def test_an_absent_room_exports_nothing_and_creates_nothing(tmp_path):
    generation, chunks = store.export_room(tmp_path, "ghost")
    assert (generation, b"".join(chunks)) == (0, b"")
    assert not (tmp_path / "rooms").exists()


def test_an_unreadable_room_raises_rather_than_impersonating_an_empty_one(tmp_path):
    """Only FileNotFoundError is the documented empty answer. Any other open failure must
    surface — a caller copying history cannot tell a silent empty 200 from data loss
    (PR #505 review). A directory where the file should be is an open failure any uid can
    stage, root included."""
    bogus = store.room_path(tmp_path, "broken")
    bogus.mkdir(parents=True)
    with pytest.raises(OSError):
        store.export_room(tmp_path, "broken")


def test_the_generation_is_captured_beside_the_snapshot(tmp_path):
    """One call returns both, the generation read from the seq state right after the
    open — captured beside the snapshot instead of whenever the handler looked earlier;
    the open-to-read gap is the accepted residual race (PR #505 review)."""
    _fill(tmp_path, "epoch", n=1)
    generation, chunks = store.export_room(tmp_path, "epoch")
    assert generation == store.room_generation(tmp_path, "epoch") == 1
    b"".join(chunks)


def test_a_bad_name_refuses_before_the_stream_starts(tmp_path):
    """valid_name runs at the call, not at the first chunk: a refusal must become a 400,
    which is only possible while no bytes of a 200 have been promised yet."""
    with pytest.raises(StoreError):
        store.export_room(tmp_path, "NOT-A-NAME")
