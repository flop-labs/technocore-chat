"""The marker and the pass lock together enforce the service-wide reap interval."""

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from _client import _age, _competing_reap_after_marker_read

import store


@pytest.mark.parametrize("marker_exists", [False, True], ids=["first-pass", "expired"])
def test_workers_observing_a_due_marker_only_run_one_pass(tmp_path, marker_exists):
    store.append(tmp_path, "active", "bot", "keep")
    store.note_set(tmp_path, "notes", "idle", "retire")
    _age(store.note_path(tmp_path, "notes", "idle"), store.IDLE_SECONDS + 60)
    marker = tmp_path / ".reaped"
    if marker_exists:
        marker.touch()
        _age(marker, store.REAP_EVERY + 60)
    else:
        marker.unlink(missing_ok=True)

    with (
        patch.object(store, "_reap_pass", wraps=store._reap_pass) as passes,
        _competing_reap_after_marker_read(tmp_path) as raced,
    ):
        store._reap(tmp_path)
        assert raced, "the competing worker never reached the marker observation"
        assert passes.call_count == 1, "a stale observation triggered a redundant store walk"

    assert store.note_get(tmp_path, "notes", "idle") is None
    assert store.read_messages(tmp_path, "active")["last_seq"] == 1


@pytest.mark.parametrize("marker_exists", [False, True], ids=["first-pass", "expired"])
def test_a_busy_reaper_does_not_arm_or_extend_the_throttle(tmp_path, marker_exists):
    marker = tmp_path / ".reaped"
    if marker_exists:
        marker.touch()
        _age(marker, store.REAP_EVERY + 60)
    before = marker.stat().st_mtime_ns if marker_exists else None
    errors = []

    def attempt():
        try:
            store._reap(tmp_path)
        except Exception as error:
            errors.append(error)

    # A separate fd contends with the holder just as another worker process would.
    # Release before the final join even if a broken variant blocks instead of skipping.
    with store._locked(marker):
        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(5)
        queued = worker.is_alive()
    worker.join(5)
    assert not queued and not worker.is_alive(), "the contender waited for the active pass"
    assert not errors
    after = marker.stat().st_mtime_ns if marker.exists() else None
    assert after == before, "a worker that did not run a pass moved its throttle"
    assert not (tmp_path / store.USAGE_FILE).exists(), "the contender ran a pass"

    store._reap(tmp_path)
    assert marker.exists()
    assert (tmp_path / store.USAGE_FILE).exists(), "the next eligible worker did not run"


def test_a_recent_pass_still_suppresses_the_next_worker(tmp_path):
    with patch.object(store, "_reap_pass", wraps=store._reap_pass) as passes:
        store._reap(tmp_path)
        store._reap(tmp_path)
        assert passes.call_count == 1


@pytest.mark.parametrize(
    "job,marker_name", [(store._reap, ".reaped"), (store._snapshot, store.SNAPSHOTS_FILE)]
)
def test_recent_maintenance_returns_without_taking_a_lock(tmp_path, monkeypatch, job, marker_name):
    (tmp_path / marker_name).touch()

    def unexpected_lock(*args, **kwargs):
        pytest.fail("an ordinary throttled write tried to take the maintenance lock")

    monkeypatch.setattr(store, "_locked", unexpected_lock)
    job(tmp_path)


@pytest.mark.parametrize("age", [-3600, -1, 0, 299, 300, 301])
@pytest.mark.parametrize(
    "job,marker_name,work",
    [
        (store._reap, ".reaped", "_reap_pass"),
        (store._snapshot, store.SNAPSHOTS_FILE, "service_stats"),
    ],
)
def test_maintenance_keeps_the_interval_boundary(
    tmp_path, monkeypatch, age, job, marker_name, work
):
    marker = tmp_path / marker_name
    marker.touch()
    now = 1_000_000.0
    os.utime(marker, (now - age, now - age))
    monkeypatch.setattr(store, "REAP_EVERY", 300)
    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 300)
    monkeypatch.setattr(store.time, "time", lambda: now)
    with patch.object(store, work, wraps=getattr(store, work)) as ran:
        job(tmp_path)
        assert ran.call_count == int(age < 0 or age >= 300)


@pytest.mark.parametrize("failed_read", [1, 2])
def test_snapshot_stat_errors_still_skip_sampling(tmp_path, monkeypatch, failed_read):
    marker = tmp_path / store.SNAPSHOTS_FILE
    history = '{"t":1,"rooms":3}\n'
    marker.write_text(history)
    _age(marker, store.SNAPSHOT_EVERY + 60)
    stat = Path.stat
    reads = []

    def fail_stat(path, *args, **kwargs):
        if path == marker:
            reads.append(path)
            if len(reads) == failed_read:
                raise OSError("temporary marker stat failure")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_stat)
    with patch.object(store, "service_stats", wraps=store.service_stats) as ran:
        store._snapshot(tmp_path)
        assert len(reads) == failed_read
        assert ran.call_count == 0
    assert marker.read_text() == history
