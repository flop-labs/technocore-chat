"""Cross-process contract tests for the store's portable sidecar lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TIMEOUT = 10

_LOCK_CHILD = r"""
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import platform_lock
lock, acquired, release = map(pathlib.Path, sys.argv[2:])
with lock.open("a+b") as file:
    platform_lock.acquire(file)
    acquired.write_text("acquired", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
    platform_lock.release(file)
"""


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _start_lock_child(lock: Path, acquired: Path, release: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _LOCK_CHILD, str(SRC), str(lock), str(acquired), str(release)],
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"child exited before {path.name}: {stdout=} {stderr=}")
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate()
    pytest.fail(f"timed out waiting for {path.name}: {stdout=} {stderr=}")


def _finish(process: subprocess.Popen[str], release: Path) -> None:
    release.touch()
    stdout, stderr = process.communicate(timeout=TIMEOUT)
    assert process.returncode == 0, (stdout, stderr)


def test_lock_excludes_another_process_until_release(tmp_path):
    lock = tmp_path / "shared.lock"
    first_acquired = tmp_path / "first-acquired"
    first_release = tmp_path / "first-release"
    second_acquired = tmp_path / "second-acquired"
    second_release = tmp_path / "second-release"
    first = _start_lock_child(lock, first_acquired, first_release)
    second = None
    try:
        _wait_for(first_acquired, first)
        second = _start_lock_child(lock, second_acquired, second_release)
        time.sleep(0.25)
        assert not second_acquired.exists(), "second process entered while the lock was held"
        _finish(first, first_release)
        _wait_for(second_acquired, second)
        _finish(second, second_release)
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


def test_store_lock_releases_after_exception(tmp_path):
    target = tmp_path / "room.jsonl"
    script = r"""
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import store
try:
    with store._locked(pathlib.Path(sys.argv[2])):
        raise RuntimeError("expected")
except RuntimeError:
    pathlib.Path(sys.argv[3]).write_text("released", encoding="utf-8")
"""
    marker = tmp_path / "exception-caught"
    result = subprocess.run(
        [sys.executable, "-c", script, str(SRC), str(target), str(marker)],
        cwd=ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.exists()

    acquired = tmp_path / "after-exception"
    release = tmp_path / "after-exception-release"
    process = _start_lock_child(target.with_suffix(".jsonl.lock"), acquired, release)
    try:
        _wait_for(acquired, process)
        _finish(process, release)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_operating_system_releases_lock_when_holder_is_terminated(tmp_path):
    lock = tmp_path / "terminated.lock"
    held = tmp_path / "held"
    never_release = tmp_path / "never-release"
    holder = _start_lock_child(lock, held, never_release)
    waiter = None
    try:
        _wait_for(held, holder)
        acquired = tmp_path / "acquired-after-termination"
        release = tmp_path / "waiter-release"
        waiter = _start_lock_child(lock, acquired, release)
        time.sleep(0.25)
        assert not acquired.exists()
        holder.kill()
        holder.communicate(timeout=TIMEOUT)
        _wait_for(acquired, waiter)
        _finish(waiter, release)
    finally:
        for process in (holder, waiter):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


def test_independent_store_processes_preserve_contiguous_sequences(tmp_path):
    script = r"""
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import store
root = pathlib.Path(sys.argv[2])
for i in range(int(sys.argv[3])):
    store.append(root, "process-race", "bot", f"{sys.argv[4]}-{i}")
"""
    # Create the room before racing so this test isolates append serialization rather than
    # room-capacity creation and events-room behavior.
    import store

    store.append(tmp_path, "process-race", "bot", "initial")
    count = 15
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(SRC), str(tmp_path), str(count), str(i)],
            cwd=ROOT,
            env=_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(4)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=TIMEOUT)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

    path = store.room_path(tmp_path, "process-race")
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["seq"] for record in records] == list(range(1, 4 * count + 2))
