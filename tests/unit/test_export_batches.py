"""Ephemeral filtering must keep exports byte-exact without one thread hop per record."""

import math
from datetime import UTC, datetime

import orjson
import pytest

import store


def _line(seq, timestamp=60, text="a short message"):
    record = {
        "seq": seq,
        "ts": datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "from": "bot",
        "text": text,
    }
    return orjson.dumps(record) + b"\n"


def _room(root, room, data):
    path = store.room_path(root, room)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _cutoff(monkeypatch):
    monkeypatch.setattr(store, "_cutoff", lambda room: 50 if store.is_ephemeral(room) else None)


def test_an_ephemeral_export_batches_records_before_the_async_transport(tmp_path, monkeypatch):
    """StreamingResponse dispatches each next() through the threadpool; a 60k-record
    ring must not require 60k dispatches merely because its expiry is checked."""
    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", 512)
    data = b"".join(_line(seq) for seq in range(1, 1001))
    _room(tmp_path, "e-batched", data)

    _, stream = store.export_room(tmp_path, "e-batched")
    chunks = list(stream)
    assert b"".join(chunks) == data
    assert len(chunks) <= math.ceil(len(data) / store.EXPORT_CHUNK)
    assert max(map(len, chunks)) <= store.EXPORT_CHUNK + max(map(len, data.splitlines())) + 1


@pytest.mark.parametrize("chunk_size", [1, 7, 32, 128, 65536])
@pytest.mark.parametrize("ephemeral", [False, True])
def test_batch_boundaries_preserve_raw_lines_and_filter_each_timestamp(
    tmp_path, monkeypatch, chunk_size, ephemeral
):
    """A batch can end inside a multibyte code point or a JSON record. Out-of-order
    timestamps and malformed lines must not turn that boundary into data loss."""
    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", chunk_size)
    live = [_line(1, text="日本語 🙂"), _line(3, text="x" * 900), _line(5)]
    lines = [live[0], _line(2, 40), live[1], b"not JSON\n", _line(4, 40), live[2]]
    data = b"".join(lines)
    room = "e-boundaries" if ephemeral else "boundaries"
    _room(tmp_path, room, data + b'{"seq":6,"text":"unfinished')

    _, stream = store.export_room(tmp_path, room)
    assert b"".join(stream) == (b"".join(live) if ephemeral else data)


@pytest.mark.parametrize("replace", [False, True])
def test_an_ephemeral_batch_keeps_its_open_snapshot_during_later_writes(
    tmp_path, monkeypatch, replace
):
    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)
    lines = [_line(1), _line(2, 40), _line(3), _line(4, 40), _line(5)]
    data = b"".join(lines)
    path = _room(tmp_path, "e-snapshot", data + b'{"seq":6,"text":"unfinished')
    _, stream = store.export_room(tmp_path, "e-snapshot")
    first = next(stream)
    later = _line(7, text="after the snapshot")
    if replace:
        store._replace(path, later)
    else:
        with path.open("ab") as target:
            target.write(b'"}\n' + later)
    assert first + b"".join(stream) == lines[0] + lines[2] + lines[4]


def test_expired_batches_need_at_most_one_empty_handoff(tmp_path, monkeypatch):
    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)
    _room(tmp_path, "e-empty-batches", b"".join(_line(seq, 40) for seq in range(1, 30)))
    _, stream = store.export_room(tmp_path, "e-empty-batches")
    chunks = list(stream)
    assert b"".join(chunks) == b""
    assert len(chunks) <= 1


@pytest.mark.parametrize("whitespace", [b"\r", b" \r\t"])
def test_json_whitespace_does_not_split_an_export_record(tmp_path, monkeypatch, whitespace):
    """JSON permits CR between members. JSONL's record delimiter remains LF only."""
    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", 8)
    ordinary = _line(1).replace(b',"ts"', b"," + whitespace + b'"ts"')
    crlf = _line(2).removesuffix(b"\n") + b"\r\n"
    data = ordinary + crlf
    _room(tmp_path, "e-json-whitespace", data)
    assert [
        record["seq"] for record in store.read_messages(tmp_path, "e-json-whitespace")["messages"]
    ] == [1, 2]
    _, stream = store.export_room(tmp_path, "e-json-whitespace")
    assert b"".join(stream) == data


def test_cr_separated_json_fragments_are_not_promoted_to_records(tmp_path, monkeypatch):
    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)
    malformed = _line(1).removesuffix(b"\n") + b"\r" + _line(2)
    live = _line(3)
    _room(tmp_path, "e-fragments", malformed + live)
    _, stream = store.export_room(tmp_path, "e-fragments")
    assert b"".join(stream) == live


def test_a_prefix_read_error_is_reported_before_returning_an_export(tmp_path, monkeypatch):
    """Prime the batching iterator rather than moving an existing eager error past the
    HTTP headers. The tail snapshot succeeds; reading the expired prefix raises EIO."""
    import errno
    from pathlib import Path

    _cutoff(monkeypatch)
    monkeypatch.setattr(store, "EXPORT_CHUNK", 32)
    path = _room(tmp_path, "e-read-error", _line(1, 40) + _line(2))
    real_open = Path.open
    opened = []

    class PrefixFailure:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def __getattr__(self, name):
            return getattr(self.source, name)

        def read(self, size=-1):
            if self.source.tell() == 0:
                raise OSError(errno.EIO, "cannot read prefix")
            return self.source.read(size)

        def readline(self, size=-1):
            if self.source.tell() == 0:
                raise OSError(errno.EIO, "cannot read prefix")
            return self.source.readline(size)

    def open_with_error(self, *args, **kwargs):
        source = real_open(self, *args, **kwargs)
        if self == path and args == ("rb",):
            opened.append(source)
            return PrefixFailure(source)
        return source

    monkeypatch.setattr(Path, "open", open_with_error)
    with pytest.raises(OSError, match="cannot read prefix"):
        store.export_room(tmp_path, "e-read-error")
    assert len(opened) == 1 and opened[0].closed
