"""Creation failures must never turn a partial seed into a persistent identity."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

SEED = "ab" * 32


@pytest.fixture
def signer():
    path = Path(__file__).resolve().parents[2] / "scripts" / "sign.py"
    spec = importlib.util.spec_from_file_location("seed_file_signer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def keygen(signer, monkeypatch, path):
    monkeypatch.setattr(sys, "argv", ["sign.py", "keygen", "--seed-file", str(path)])
    monkeypatch.setattr(signer.secrets, "token_hex", lambda size: SEED)
    signer.main()


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "close"])
def test_failed_seed_creation_leaves_no_final_or_staging_file(
    signer, monkeypatch, tmp_path, capsys, failure
):
    path = tmp_path / "identity.seed"
    real_fdopen = os.fdopen

    @contextmanager
    def faulty_fdopen(*args, **kwargs):
        with real_fdopen(*args, **kwargs) as destination:
            proxy = Mock(wraps=destination)

            def partial_write(text):
                destination.write(text[:9])
                destination.flush()
                raise OSError("injected partial write")

            if failure == "write":
                proxy.write.side_effect = partial_write
            elif failure == "flush":
                proxy.flush.side_effect = OSError("injected flush failure")
            yield proxy
            if failure == "close":
                raise OSError("injected close failure")

    monkeypatch.setattr(os, "fdopen", faulty_fdopen)
    if failure == "fsync":
        monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("injected fsync failure")))
    with pytest.raises(SystemExit):
        keygen(signer, monkeypatch, path)
    assert capsys.readouterr().out == ""
    assert not path.exists(), "a failed keygen left a readable identity at the final path"
    assert list(tmp_path.iterdir()) == [], "private staging material was not cleaned up"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
def test_seed_creation_syncs_parent_directory_before_reporting_success(
    signer, monkeypatch, tmp_path, capsys
):
    path = tmp_path / "identity.seed"
    real_fsync = os.fsync
    synced = []

    def observed_fsync(fd):
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        if kind == "directory":
            assert path.read_text(encoding="utf-8") == SEED + "\n"
            assert list(tmp_path.iterdir()) == [path]
            assert capsys.readouterr().out == "", "keygen reported success before directory fsync"
        real_fsync(fd)
        synced.append(kind)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    keygen(signer, monkeypatch, path)
    assert synced == ["file", "directory"]
    output = capsys.readouterr().out
    assert "did: " in output and SEED not in output


def test_final_seed_is_absent_until_file_sync_completes(signer, monkeypatch, tmp_path):
    path = tmp_path / "identity.seed"
    real_fsync = os.fsync
    checked = []

    def observed_fsync(fd):
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            assert not path.exists()
            if os.name != "nt":
                assert stat.S_IMODE(mode) == 0o600
            checked.append(True)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    signer.write_seed_file(path, SEED)
    assert checked == [True]
    assert signer.read_seed_file(path) == SEED
    assert list(tmp_path.iterdir()) == [path]


def test_seed_publication_preserves_a_competing_identity(signer, monkeypatch, tmp_path, capsys):
    path = tmp_path / "identity.seed"
    other = "cd" * 32 + "\n"
    real_fsync = os.fsync

    def competing_creator(fd):
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            path.write_text(other, encoding="utf-8")

    monkeypatch.setattr(os, "fsync", competing_creator)
    with pytest.raises(SystemExit):
        keygen(signer, monkeypatch, path)
    assert capsys.readouterr().out == ""
    assert path.read_text(encoding="utf-8") == other
    assert list(tmp_path.iterdir()) == [path]


def test_failed_fdopen_closes_and_removes_staging_file(signer, monkeypatch, tmp_path):
    descriptors = []

    def failed_fdopen(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(os, "fdopen", failed_fdopen)
    with pytest.raises(SystemExit):
        signer.write_seed_file(tmp_path / "identity.seed", SEED)
    assert list(tmp_path.iterdir()) == []
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_failed_cleanup_reports_private_staging_file(signer, monkeypatch, tmp_path, capsys):
    path = tmp_path / "identity.seed"
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("injected fsync failure")))
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=OSError("injected cleanup failure")))
    with pytest.raises(SystemExit, match="could not remove private staging file"):
        keygen(signer, monkeypatch, path)
    assert capsys.readouterr().out == ""
    assert not path.exists()
    remaining = list(tmp_path.iterdir())
    assert len(remaining) == 1 and remaining[0].name.startswith(".seed-")
    if os.name != "nt":
        assert stat.S_IMODE(remaining[0].stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
@pytest.mark.parametrize("failure", ["open", "fsync"])
def test_directory_failure_preserves_complete_key_without_reporting_success(
    signer, monkeypatch, tmp_path, capsys, failure
):
    path = tmp_path / "identity.seed"
    real_open, real_fsync = os.open, os.fsync

    def failed_open(name, *args, **kwargs):
        if Path(name) == tmp_path:
            raise OSError("injected directory open failure")
        return real_open(name, *args, **kwargs)

    def failed_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    if failure == "open":
        monkeypatch.setattr(os, "open", failed_open)
    else:
        monkeypatch.setattr(os, "fsync", failed_fsync)
    with pytest.raises(SystemExit, match="durability is uncertain"):
        keygen(signer, monkeypatch, path)
    assert capsys.readouterr().out == ""
    assert path.read_bytes() == (SEED + "\n").encode()
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link publication")
def test_unavailable_hard_links_fail_without_copying_to_final_path(signer, monkeypatch, tmp_path):
    monkeypatch.setattr(os, "link", Mock(side_effect=OSError("hard links unavailable")))
    with pytest.raises(SystemExit):
        signer.write_seed_file(tmp_path / "identity.seed", SEED)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fail_move", [False, True])
def test_windows_uses_write_through_without_replacing_or_copying(
    signer, monkeypatch, tmp_path, capsys, fail_move
):
    if os.name != "nt":
        pytest.skip("native Windows publication")
    import ctypes

    path = tmp_path / "identity.seed"
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    native_move = library.MoveFileExW
    calls = []

    def observed_move(source, target, flags):
        assert flags == 0x8  # WRITE_THROUGH, with neither REPLACE_EXISTING nor COPY_ALLOWED.
        assert Path(source).read_text(encoding="utf-8") == SEED + "\n"
        assert not path.exists()
        assert capsys.readouterr().out == ""
        calls.append(flags)
        if fail_move:
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
            return 0
        # The wrapper receives ctypes signatures from the signer; give the real
        # native call those same types so this exercises Windows rather than a fake move.
        native_move.argtypes = move.argtypes
        native_move.restype = move.restype
        return native_move(source, target, flags)

    move = Mock(side_effect=observed_move)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Mock(MoveFileExW=move))
    if fail_move:
        with pytest.raises(SystemExit):
            keygen(signer, monkeypatch, path)
        assert capsys.readouterr().out == ""
        assert list(tmp_path.iterdir()) == []
    else:
        keygen(signer, monkeypatch, path)
        assert "did: " in capsys.readouterr().out
        assert signer.read_seed_file(path) == SEED
        assert list(tmp_path.iterdir()) == [path]
    assert calls == [0x8]
