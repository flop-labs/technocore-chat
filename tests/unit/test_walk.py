"""Root confinement for store directory walks.

The reaper used to classify a child with `is_dir()` and then reopen it by
pathname. Replacing that directory with a symlink in the window let `_reap`
unlink files outside the store root. These tests pin the public reaper path,
not the walker alone.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _client import _age


def _can_symlink(tmp_path: Path) -> bool:
    target = tmp_path / "symlink-target"
    target.mkdir()
    link = tmp_path / "symlink"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return link.is_symlink() or os.path.islink(link)


def _arm_reaper(root: Path) -> None:
    (root / ".reaped").unlink(missing_ok=True)


def test_walk_skips_an_existing_directory_symlink(tmp_path: Path) -> None:
    import store
    import walk

    if not _can_symlink(tmp_path):
        pytest.skip("directory symlinks are unavailable")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep")
    notes = tmp_path / "notes"
    notes.mkdir()
    os.symlink(outside, notes / "trap", target_is_directory=True)
    (notes / "did").mkdir()
    (notes / "did" / "ok.txt").write_text("inside")
    found = {Path(entry.path).name for entry in walk.files(notes, ".txt")}
    assert found == {"ok.txt"}
    assert store._scan(notes, ".txt") == (1, 0)
    assert store._scan(notes, ".txt", sized=True) == (1, 6)


def test_walk_follows_a_symlinked_store_root(tmp_path: Path) -> None:
    import walk

    if not _can_symlink(tmp_path):
        pytest.skip("directory symlinks are unavailable")
    real = tmp_path / "volume"
    real.mkdir()
    (real / "ok.txt").write_text("mounted")
    alias = tmp_path / "store"
    os.symlink(real, alias, target_is_directory=True)
    found = [Path(entry.path).name for entry in walk.files(alias, ".txt")]
    assert found == ["ok.txt"]


def test_walk_skips_trees_deeper_than_max_depth(tmp_path: Path) -> None:
    import walk

    current = tmp_path
    for i in range(walk.MAX_DEPTH + 2):
        current = current / f"d{i}"
        current.mkdir()
    (current / "too-deep.txt").write_text("no")
    shallow = tmp_path / "d0" / "ok.txt"
    shallow.write_text("yes")
    found = {Path(entry.path).name for entry in walk.files(tmp_path, ".txt")}
    assert found == {"ok.txt"}


def test_walk_skips_a_vanished_child_and_a_stale_sized_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import walk

    nested = tmp_path / "gone"
    nested.mkdir()
    live = tmp_path / "keep.txt"
    live.write_text("keep")
    stale = tmp_path / "stale.txt"
    stale.write_text("drop")

    def vanish(full_path: str, name: str) -> None:
        if name == "gone":
            Path(full_path).rmdir()

    monkeypatch.setattr(walk, "_before_open_child", vanish)
    assert {Path(entry.path).name for entry in walk.files(tmp_path, ".txt")} == {
        "keep.txt",
        "stale.txt",
    }
    stale.unlink()
    assert walk.counts(tmp_path, ".txt", sized=True) == (1, 4)


def test_reap_does_not_follow_a_directory_replaced_by_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check-to-open window: a real child directory becomes a symlink before
    recursive open. The public reaper must not delete the external target, and it
    must still reap a later legitimate sibling."""
    import store
    import walk

    if not _can_symlink(tmp_path):
        pytest.skip("directory symlinks are unavailable")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "keep-me.txt"
    victim.write_text("external")
    _age(victim, store.IDLE_SECONDS + 60)
    store.note_set(tmp_path, "did", "keep", "live")
    trap = tmp_path / "notes" / "trap"
    trap.mkdir()
    (trap / "decoy.txt").write_text("decoy")
    stale = store.note_path(tmp_path, "old", "gone")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale")
    _age(stale, store.IDLE_SECONDS + 60)

    def swap(full_path: str, name: str) -> None:
        if name != "trap":
            return
        decoy = Path(full_path) / "decoy.txt"
        decoy.unlink(missing_ok=True)
        Path(full_path).rmdir()
        os.symlink(outside, full_path, target_is_directory=True)

    monkeypatch.setattr(walk, "_before_open_child", swap)
    _arm_reaper(tmp_path)
    store._reap(tmp_path)
    assert victim.exists() and victim.read_text() == "external"
    assert not stale.exists()
    assert store.note_get(tmp_path, "did", "keep") == "live"
    assert (tmp_path / "notes" / "trap").is_symlink()
