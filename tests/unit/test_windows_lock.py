"""Windows lock regression (#255): store must not import fcntl unconditionally.

Fails on base: `import fcntl` sits at module top level, so a simulated Windows
environment (os.name == "nt", fcntl blocked) raises ModuleNotFoundError on import.

The import is only half of #255. The other half is the *waiting* contract, which is not an
import bug and which an import-only test cannot see: `msvcrt`'s own waiting modes give up
after ten attempts, where `flock` waits until it gets the lock. Those cases run everywhere
by driving a recording stand-in; `test_blocking_lock_outlives_the_crt_retry_window` is the
end-to-end one and needs a real Windows, so it skips elsewhere.
"""

from __future__ import annotations

import ast
import errno
import importlib
import os
import sys
import threading
import time
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "store.py"

# `_locking`'s waiting modes re-attempt once a second and give up after ten attempts, so
# the shortest holder that can tell the two contracts apart is one that outlives ~10s.
# Two seconds of margin on top of the documented window.
_CRT_RETRY_WINDOW_S = 10.0
_HOLD_S = _CRT_RETRY_WINDOW_S + 2.0

_LK_LOCK, _LK_NBLCK, _LK_RLCK, _LK_NBRLCK, _LK_UNLCK = 1, 2, 3, 4, 0
_WAITING_MODES = (_LK_LOCK, _LK_RLCK)


def _recording_msvcrt(
    busy_for: int = 0, permanent: int | None = None
) -> tuple[Any, list[tuple[int, int]]]:
    """A stand-in for `msvcrt` that records calls and can hold the region busy.

    `busy_for` is how many attempts fail with a busy region before one succeeds, which is
    the only way to reach the retry path without a second process.

    `permanent` raises that errno on every attempt instead: a failure that will never become
    a lock, as distinct from contention. EBADF is what a closed handle gives and EINVAL what
    an unsupported lock or filesystem gives — both plain `OSError`, where a busy region is
    EACCES and therefore `PermissionError`.
    """
    calls: list[tuple[int, int]] = []
    busy = [busy_for]
    m: Any = types.ModuleType("msvcrt")
    for name, value in (
        ("LK_LOCK", _LK_LOCK),
        ("LK_NBLCK", _LK_NBLCK),
        ("LK_RLCK", _LK_RLCK),
        ("LK_NBRLCK", _LK_NBRLCK),
        ("LK_UNLCK", _LK_UNLCK),
    ):
        setattr(m, name, value)

    def locking(fd: int, mode: int, nbytes: int) -> None:
        calls.append((mode, nbytes))
        assert mode not in _WAITING_MODES, (
            f"locking(mode={mode}) waits boundedly *inside the CRT* and raises after ten "
            "attempts, so a holder running longer than that fails the next caller instead "
            "of queueing it; the wait has to be polled with LK_NBLCK/LK_NBRLCK"
        )
        if mode == _LK_UNLCK:
            return
        if permanent is not None:
            raise OSError(permanent, os.strerror(permanent))
        if busy[0] > 0:
            busy[0] -= 1
            raise OSError(errno.EACCES, "the region is held")

    m.locking = locking
    return m, calls


@contextmanager
def _simulated_windows(monkeypatch, msvcrt: Any) -> Iterator[Any]:
    """Import `store` as it would be on Windows: `os.name == "nt"`, no `fcntl`."""
    real_store = sys.modules.pop("store", None)
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl -> ModuleNotFoundError
    monkeypatch.setattr(os, "name", "nt")
    try:
        yield importlib.import_module("store")
    finally:
        sys.modules.pop("store", None)
        if real_store is not None:
            sys.modules["store"] = real_store


def _fake_msvcrt() -> Any:
    m: Any = types.ModuleType("msvcrt")
    m.LK_LOCK = _LK_LOCK
    m.LK_NBLCK = _LK_NBLCK
    m.LK_RLCK = _LK_RLCK
    m.LK_NBRLCK = _LK_NBRLCK
    m.LK_UNLCK = _LK_UNLCK
    m.locking = lambda *a, **k: None
    return m


def test_no_top_level_fcntl_import():
    """Belt-and-braces: fcntl must not be an unconditional module-level import."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "fcntl", "top-level import fcntl breaks Windows"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "fcntl", "top-level from fcntl breaks Windows"


def test_store_imports_on_windows_without_fcntl(monkeypatch, tmp_path):
    """Simulated Windows: fcntl absent, msvcrt present — `import store` must succeed."""
    with _simulated_windows(monkeypatch, _fake_msvcrt()) as store:
        assert store.os.name == "nt"
        with store._locked(tmp_path / "probe", shared=False, nb=True):
            pass


def test_blocking_lock_polls_the_nonblocking_mode(monkeypatch, tmp_path):
    """Uncontended: one non-blocking attempt and an unlock, never a CRT waiting mode."""
    fake, calls = _recording_msvcrt()
    with _simulated_windows(monkeypatch, fake) as store:
        with store._locked(tmp_path / "probe"):
            pass
    assert [mode for mode, _ in calls] == [_LK_NBLCK, _LK_UNLCK]


def test_blocking_lock_waits_out_a_busy_region(monkeypatch, tmp_path):
    """Busy is retried, not raised — the loop is the waiting half of the contract."""
    fake, calls = _recording_msvcrt(busy_for=3)
    with _simulated_windows(monkeypatch, fake) as store:
        with store._locked(tmp_path / "probe"):
            pass
    assert [mode for mode, _ in calls] == [_LK_NBLCK] * 4 + [_LK_UNLCK]


def test_shared_lock_polls_the_read_mode(monkeypatch, tmp_path):
    """`shared` has to reach the primitive, or `_create_gate` is not shared at all."""
    fake, calls = _recording_msvcrt()
    with _simulated_windows(monkeypatch, fake) as store:
        with store._locked(tmp_path / "probe", shared=True):
            pass
    assert [mode for mode, _ in calls] == [_LK_NBRLCK, _LK_UNLCK]


def test_nonblocking_lock_reports_busy_as_eagain_without_waiting(monkeypatch, tmp_path):
    """`nb` gives up on the first refusal, as BlockingIOError(EAGAIN) — `_reap` catches it."""
    fake, calls = _recording_msvcrt(busy_for=1)
    with _simulated_windows(monkeypatch, fake) as store:
        with pytest.raises(BlockingIOError) as caught:
            with store._locked(tmp_path / "probe", nb=True):
                pass
    assert caught.value.errno == errno.EAGAIN
    # One attempt of one byte, and no unlock of a lock that was never taken.
    assert [mode for mode, _ in calls] == [_LK_NBLCK]


def test_a_permanent_error_is_raised_rather_than_retried(monkeypatch, tmp_path):
    """Only a busy region is contention; EBADF never becomes a lock however long you wait.

    `locking` reports both through `OSError`, so catching the family whole turns a closed
    handle into an infinite retry loop on the blocking path — the loop would never exit and
    no caller would ever see the error. The errno has to be classified, not just caught.
    """
    fake, calls = _recording_msvcrt(permanent=errno.EBADF)
    slept: list[float] = []
    with _simulated_windows(monkeypatch, fake) as store:
        monkeypatch.setattr(store.time, "sleep", slept.append)
        with pytest.raises(OSError) as caught:
            with store._locked(tmp_path / "probe"):
                pass
    assert caught.value.errno == errno.EBADF
    assert [mode for mode, _ in calls] == [_LK_NBLCK], "a permanent error was retried"
    assert slept == [], "a permanent error was waited on"


def test_a_permanent_error_is_not_reported_as_eagain(monkeypatch, tmp_path):
    """`nb` translates contention into EAGAIN. A permanent error must not borrow that mask.

    `_bump` and `_reap` treat BlockingIOError as "someone else holds it, carry on", so a
    mislabelled EBADF is a real failure silently downgraded to routine contention — worse
    than the retry loop, because nothing looks wrong.
    """
    fake, calls = _recording_msvcrt(permanent=errno.EINVAL)
    with _simulated_windows(monkeypatch, fake) as store:
        with pytest.raises(OSError) as caught:
            with store._locked(tmp_path / "probe", nb=True):
                pass
    assert caught.value.errno == errno.EINVAL
    assert not isinstance(caught.value, BlockingIOError), "a permanent error was masked"
    assert [mode for mode, _ in calls] == [_LK_NBLCK]


def test_windows_lock_poll_interval_is_sane(monkeypatch):
    """A retry loop is only a lock if it sleeps: too fine spins, too coarse is a stall."""
    with _simulated_windows(monkeypatch, _fake_msvcrt()) as store:
        assert 0.001 <= store._WIN_LOCK_POLL_S <= 0.5


@pytest.mark.skipif(os.name != "nt", reason="exercises the real msvcrt primitive")
def test_blocking_lock_outlives_the_crt_retry_window(tmp_path):
    """A waiter must queue behind a long holder rather than give up at ten attempts.

    `msvcrt.LK_LOCK` / `LK_RLCK` re-attempt once a second and raise OSError after ten
    attempts — a bounded wait. `flock(LOCK_EX)` waits until it gets the lock, and `_locked`
    is the primitive callers use *because* they intend to wait the current holder out
    (`_reap` rewrites a count from a walk; `_create_gate` removes a directory a create is
    entering). A holder running longer than the CRT window must not turn the next caller
    into an error.

    Measured, not assumed: against the LK_LOCK implementation this fails with
    OSError(EDEADLK) at ~9.4s; against the polling implementation it passes. The `waited`
    assertion keeps it honest — it fails if the lock was never actually contended, so a
    later edit cannot quietly turn this into a pass.
    """
    import store

    target = tmp_path / "probe"
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store._locked(target):
            held.set()
            release.wait(_HOLD_S * 2)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5.0), "the holder never took the lock"

    try:
        start = time.monotonic()
        with store._locked(target):
            pass
        waited = time.monotonic() - start
    finally:
        release.set()
        holder.join(10.0)

    assert not holder.is_alive(), "the holder never released the lock"
    assert waited > _CRT_RETRY_WINDOW_S, (
        f"the waiter acquired after {waited:.1f}s — inside the CRT retry window, so it "
        "never actually contended and this run proves nothing"
    )
