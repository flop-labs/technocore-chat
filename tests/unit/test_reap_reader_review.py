"""PR #126 review: real reap at the open boundary, plus HTTP and error controls."""

import os
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app
import config
import store


def _prepare(root, kind):
    if kind == "note":
        store.note_set(root, "plans", "gone", "synthetic note")
        target = store.note_path(root, "plans", "gone")
    else:
        store.append(root, "gone", "bot", "synthetic message")
        target = store.room_path(root, "gone")
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(target, (old, old))
    return target


def _reap_just_before_open(root, target, monkeypatch):
    real_open = Path.open
    fired = []

    def open_after_reap(path, *args, **kwargs):
        if path == target and not fired:
            fired.append(True)
            (root / ".reaped").unlink(missing_ok=True)
            store._reap(root)
            assert not target.exists(), "the real reaper must remove the idle target"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_after_reap)
    return fired


@pytest.mark.parametrize("reader", ["messages", "last-seq", "window", "note", "export"])
def test_real_reap_during_read(tmp_path, monkeypatch, reader):
    target = _prepare(tmp_path, "note" if reader == "note" else "room")
    fired = _reap_just_before_open(tmp_path, target, monkeypatch)
    if reader == "messages":
        value = store.read_messages(tmp_path, "gone")
        assert value["messages"] == []
        assert value["generation"] == 1
    elif reader == "last-seq":
        assert store.last_seq(tmp_path, "gone") == 1, "preserve the reaper's retained floor"
    elif reader == "window":
        assert store.room_window(tmp_path, "gone") == (0, [])
    elif reader == "note":
        assert store.note_get(tmp_path, "plans", "gone") is None
    else:
        generation, chunks = store.export_room(tmp_path, "gone")
        assert generation == 1 and b"".join(chunks) == b""
    assert fired == [True]


@pytest.mark.parametrize("kind", ["room", "note"])
def test_http_read_while_reaper_removes_target(tmp_path, monkeypatch, kind):
    target = _prepare(tmp_path, kind)
    fired = _reap_just_before_open(tmp_path, target, monkeypatch)
    with (
        config.override(ROOT=tmp_path),
        TestClient(app.app, raise_server_exceptions=False) as client,
    ):
        url = "/r/gone?format=json" if kind == "room" else "/kv/plans/gone"
        response = client.get(url)
    assert fired == [True]
    assert response.status_code == (200 if kind == "room" else 404)
    if kind == "room":
        assert response.json()["messages"] == []


@pytest.mark.parametrize("reader", ["messages", "last-seq", "window", "note", "export"])
def test_permission_error_does_not_become_an_empty_result(tmp_path, monkeypatch, reader):
    target = _prepare(tmp_path, "note" if reader == "note" else "room")
    real_open = Path.open

    def forbidden(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(PermissionError, match="synthetic denial"):
        if reader == "messages":
            store.read_messages(tmp_path, "gone")
        elif reader == "last-seq":
            store.last_seq(tmp_path, "gone")
        elif reader == "window":
            store.room_window(tmp_path, "gone")
        elif reader == "note":
            store.note_get(tmp_path, "plans", "gone")
        else:
            store.export_room(tmp_path, "gone")


def test_a_held_descriptor_keeps_its_original_snapshot(tmp_path, monkeypatch):
    """A reap after open must not invalidate an already-held room descriptor."""
    target = _prepare(tmp_path, "room")
    real_open = Path.open
    fired = []

    def open_before_reap(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path == target and not fired:
            fired.append(True)
            (tmp_path / ".reaped").unlink(missing_ok=True)
            store._reap(tmp_path)
            assert not target.exists()
        return handle

    monkeypatch.setattr(Path, "open", open_before_reap)
    view = store.read_messages(tmp_path, "gone")
    assert fired == [True]
    assert [message["text"] for message in view["messages"]] == ["synthetic message"]
