"""A failed history read must not authorize replacing the retained snapshots."""

import errno
import os
from pathlib import Path

import orjson
import pytest


@pytest.fixture
def sampling(monkeypatch):
    import store

    calls = {"stats": 0}

    def collect(_root):
        calls["stats"] += 1
        return {"counters": {"messages": 300}}

    monkeypatch.setattr(store, "_bump", lambda _root: None)
    monkeypatch.setattr(store, "service_stats", collect)
    return store, calls


def _seed(root, store):
    """Both samples are retained, but their file is due for another sampling pass."""
    now = int(store.time.time())
    history = [
        {"t": now - 900, "counters": {"messages": 100}},
        {"t": now - 600, "counters": {"messages": 200}},
    ]
    path = root / store.SNAPSHOTS_FILE
    path.write_bytes(b"".join(orjson.dumps(record) + b"\n" for record in history))
    old = now - store.SNAPSHOT_EVERY - 60
    os.utime(path, (old, old))
    return path, history


def _seed_invalid_utf8(root, store):
    """A decode failure after two healthy samples must not erase those samples."""
    path, history = _seed(root, store)
    before_mtime = path.stat().st_mtime_ns
    path.write_bytes(path.read_bytes() + b"\xff\n")
    os.utime(path, ns=(before_mtime, before_mtime))
    return path, history


def _fail_one_read(monkeypatch, target, code):
    """Only the history read fails; recovery and all other filesystem IO stay real."""
    original = Path.read_text
    failures = []

    def read(path, *args, **kwargs):
        if path == target and not failures:
            failures.append(code)
            raise OSError(code, os.strerror(code), str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return failures


@pytest.mark.parametrize(
    "code", [errno.EIO, errno.EMFILE, errno.EACCES], ids=["EIO", "EMFILE", "EACCES"]
)
def test_a_failed_history_read_does_not_replace_history(tmp_path, monkeypatch, sampling, code):
    store, calls = sampling
    path, history = _seed(tmp_path, store)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    failures = _fail_one_read(monkeypatch, path, code)

    store._snapshot(tmp_path)

    assert failures == [code], "the intended history read was not reached"
    assert path.read_bytes() == before, "one failed read erased healthy retained history"
    assert path.stat().st_mtime_ns == before_mtime, "a failed read reset the throttle"
    assert calls["stats"] == 0, "unreadable history must stop before collecting a replacement"

    # No clock or mtime change: a skipped sample stays due and can recover immediately.
    store._snapshot(tmp_path)
    recovered = store.snapshots(tmp_path)
    assert recovered[:2] == history
    assert len(recovered) == 3
    assert recovered[-1]["counters"]["messages"] == 300
    assert calls["stats"] == 1


@pytest.mark.parametrize(
    "code", [errno.EIO, errno.EMFILE, errno.EACCES], ids=["EIO", "EMFILE", "EACCES"]
)
def test_an_unreadable_history_is_not_an_empty_history(tmp_path, monkeypatch, sampling, code):
    store, _ = sampling
    path, history = _seed(tmp_path, store)
    before = path.read_bytes()
    failures = _fail_one_read(monkeypatch, path, code)
    with pytest.raises(OSError) as caught:
        store.snapshots(tmp_path)
    assert caught.value.errno == code
    assert failures == [code]
    assert path.read_bytes() == before
    assert store.snapshots(tmp_path) == history


def test_a_missing_history_still_initializes(tmp_path, sampling):
    store, calls = sampling
    assert store.snapshots(tmp_path) == []
    store._snapshot(tmp_path)
    result = store.snapshots(tmp_path)
    assert len(result) == 1 and result[0]["counters"]["messages"] == 300
    assert calls["stats"] == 1


def test_a_healthy_history_appends_and_throttles(tmp_path, sampling):
    store, calls = sampling
    _, history = _seed(tmp_path, store)
    store._snapshot(tmp_path)
    result = store.snapshots(tmp_path)
    assert result[:2] == history and len(result) == 3
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path) == result
    assert calls["stats"] == 1


def test_a_torn_json_line_still_costs_only_that_line(tmp_path, sampling):
    store, _ = sampling
    path, history = _seed(tmp_path, store)
    before_mtime = path.stat().st_mtime_ns
    with path.open("ab") as stream:
        stream.write(b'{"t":')
    os.utime(path, ns=(before_mtime, before_mtime))
    assert store.snapshots(tmp_path) == history
    store._snapshot(tmp_path)
    result = store.snapshots(tmp_path)
    assert result[:2] == history and len(result) == 3


def test_an_aggregate_read_error_keeps_history(tmp_path, monkeypatch, sampling):
    store, _ = sampling
    path, history = _seed(tmp_path, store)
    before = path.read_bytes()

    def collect(_root):
        raise OSError(errno.EIO, "test-only aggregate failure")

    monkeypatch.setattr(store, "service_stats", collect)
    store._snapshot(tmp_path)
    assert path.read_bytes() == before
    assert store.snapshots(tmp_path) == history


def test_a_busy_sampler_keeps_history(tmp_path, sampling):
    store, calls = sampling
    path, history = _seed(tmp_path, store)
    before = path.read_bytes()
    with store._locked(path):
        store._snapshot(tmp_path)
    assert path.read_bytes() == before
    assert store.snapshots(tmp_path) == history
    assert calls["stats"] == 0


def test_a_failed_replace_keeps_history(tmp_path, monkeypatch, sampling):
    store, _ = sampling
    path, history = _seed(tmp_path, store)
    before = path.read_bytes()

    def fail_replace(_path, _data):
        raise OSError(errno.EIO, "test-only replacement failure")

    monkeypatch.setattr(store, "_replace", fail_replace)
    store._snapshot(tmp_path)
    assert path.read_bytes() == before
    assert store.snapshots(tmp_path) == history


@pytest.mark.parametrize(
    "code", [errno.EIO, errno.EMFILE, errno.EACCES], ids=["EIO", "EMFILE", "EACCES"]
)
def test_an_append_survives_failed_sampling_without_erasing_history(tmp_path, monkeypatch, code):
    """Exercise real append, counter flushing and aggregate collection, not the stubs."""
    import store

    store.append(tmp_path, "p-snapshot-check", "bot", "before")
    path, history = _seed(tmp_path, store)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    failures = _fail_one_read(monkeypatch, path, code)

    posted = store.append(tmp_path, "p-snapshot-check", "bot", "after")

    assert failures == [code]
    assert posted["seq"] == 2 and posted["text"] == "after"
    assert store.read_messages(tmp_path, "p-snapshot-check")["count"] == 2
    assert path.read_bytes() == before, "a successful append discarded the old samples"
    assert path.stat().st_mtime_ns == before_mtime
    store._snapshot(tmp_path)
    result = store.snapshots(tmp_path)
    assert result[:2] == history and len(result) == 3
    assert result[-1]["counters"]["messages"] == 2


@pytest.mark.parametrize(
    "code", [errno.EIO, errno.EMFILE, errno.EACCES], ids=["EIO", "EMFILE", "EACCES"]
)
def test_the_stats_view_does_not_invent_an_empty_history(tmp_path, monkeypatch, code):
    """The other consumer must not publish a successful-looking history=[] on IO failure."""
    import app as app_module
    import store

    path, history = _seed(tmp_path, store)
    before = path.read_bytes()
    failures = _fail_one_read(monkeypatch, path, code)
    with pytest.raises(OSError) as caught:
        app_module._stats_view(tmp_path)
    assert caught.value.errno == code
    assert failures == [code]
    assert path.read_bytes() == before
    assert app_module._stats_view(tmp_path)["history"] == history


def test_invalid_utf8_skips_sampling_without_replacing_history(tmp_path, sampling):
    store, calls = sampling
    path, history = _seed_invalid_utf8(tmp_path, store)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    store._snapshot(tmp_path)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert calls["stats"] == 0

    # Once the unreadable bytes are repaired, the skipped pass is still due.
    path.write_bytes(b"".join(orjson.dumps(record) + b"\n" for record in history))
    os.utime(path, ns=(before_mtime, before_mtime))
    store._snapshot(tmp_path)
    recovered = store.snapshots(tmp_path)
    assert recovered[:2] == history and len(recovered) == 3
    assert calls["stats"] == 1


@pytest.mark.parametrize("reader", ["snapshots", "stats"])
def test_invalid_utf8_is_not_an_empty_history(tmp_path, sampling, reader):
    import app as app_module

    store, _ = sampling
    path, _ = _seed_invalid_utf8(tmp_path, store)
    before = path.read_bytes()
    read = store.snapshots if reader == "snapshots" else app_module._stats_view

    with pytest.raises(UnicodeDecodeError):
        read(tmp_path)

    assert path.read_bytes() == before


def test_an_append_survives_invalid_utf8_without_erasing_history(tmp_path):
    """A sampler decode error must not fail a message that has already been stored."""
    import store

    store.append(tmp_path, "p-snapshot-check", "bot", "before")
    path, _ = _seed_invalid_utf8(tmp_path, store)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    posted = store.append(tmp_path, "p-snapshot-check", "bot", "after")

    assert posted["seq"] == 2 and posted["text"] == "after"
    assert store.read_messages(tmp_path, "p-snapshot-check")["count"] == 2
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
