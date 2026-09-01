"""Tests for the lock-free sequence-numbered counter segment architecture."""

from __future__ import annotations
import orjson
from pathlib import Path

from src.store import counters, _bump, _compact_counters, COUNTERS_FILE

def test_legacy_upgrade_discovery(tmp_path: Path):
    """A pre-sharding `.counters` file is discovered as sequence -1 and preserved."""
    legacy = tmp_path / COUNTERS_FILE
    legacy.write_bytes(orjson.dumps({"messages": 9, "rooms_created": 2}) + b"\n")
    
    assert counters(tmp_path)["messages"] == 9
    
    _bump(tmp_path, messages=1)
    assert counters(tmp_path)["messages"] == 10
    assert legacy.exists()

def test_atomic_handoff_and_delayed_fold(tmp_path: Path):
    """Compaction leaves the active segment alone and only folds fully sealed older segments."""
    _bump(tmp_path, messages=5)
    _compact_counters(tmp_path) 
    
    _bump(tmp_path, messages=3)
    _compact_counters(tmp_path) 
    
    assert counters(tmp_path)["messages"] == 8

def test_crash_idempotency(tmp_path: Path):
    """If a crash leaves folded source segments on disk, a _fold_up_to marker prevents double counting."""
    seg0 = tmp_path / f"{COUNTERS_FILE}.0"
    seg1 = tmp_path / f"{COUNTERS_FILE}.1"
    
    seg0.write_bytes(orjson.dumps({"messages": 10}) + b"\n")
    seg1.write_bytes(orjson.dumps({"_fold_up_to": 0, "messages": 10}) + b"\n")
    
    assert counters(tmp_path)["messages"] == 10