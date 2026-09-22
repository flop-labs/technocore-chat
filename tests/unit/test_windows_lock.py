"""Windows lock regression (#255): store must not import fcntl unconditionally.

Fails on base: `import fcntl` sits at module top level, so a simulated Windows
environment (os.name == "nt", fcntl blocked) raises ModuleNotFoundError on import.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[2] / "src" / "store.py"


def _fake_msvcrt() -> Any:
    m: Any = types.ModuleType("msvcrt")
    m.LK_LOCK = 1
    m.LK_NBLCK = 2
    m.LK_RLCK = 3
    m.LK_NBRLCK = 4
    m.LK_UNLCK = 0
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
    real_store = sys.modules.pop("store", None)
    real_fcntl = sys.modules.get("fcntl")
    monkeypatch.setitem(sys.modules, "msvcrt", _fake_msvcrt())
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl -> ModuleNotFoundError
    monkeypatch.setattr(os, "name", "nt")
    try:
        store = importlib.import_module("store")
        assert store.os.name == "nt"
        with store._locked(tmp_path / "probe", shared=False, nb=True):
            pass
    finally:
        sys.modules.pop("store", None)
        if real_store is not None:
            sys.modules["store"] = real_store
        if real_fcntl is not None:
            sys.modules["fcntl"] = real_fcntl
        elif "fcntl" in sys.modules and sys.modules["fcntl"] is None:
            del sys.modules["fcntl"]
