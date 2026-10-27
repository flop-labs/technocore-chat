"""Deterministic unit tests for the standalone network observatory."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _observatory():
    spec = importlib.util.spec_from_file_location("observatory", ROOT / "tools" / "observatory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_rooms_keeps_valid_entries_and_reports_unmatched_lines():
    observatory = _observatory()
    raw = (ROOT / "tests" / "fixtures" / "observatory" / "rooms.txt").read_text(encoding="utf-8")

    rooms, unmatched_lines = observatory.parse_rooms(raw)

    assert rooms == [
        {
            "room": "lobby",
            "seq": 42,
            "size": "1.2K",
            "age": "3s ago",
            "topic": "welcome agents",
        },
        {
            "room": "build-log",
            "seq": 7,
            "size": "900",
            "age": "1m ago",
            "topic": None,
        },
    ]
    assert unmatched_lines == ["/r/not-a-room line with an unexpected shape"]


def test_classify_room_uses_message_count_and_agent_ratio():
    observatory = _observatory()

    assert observatory.classify_room({"messages": 0, "unique_agents": 0}) == "EMPTY"
    assert observatory.classify_room({"messages": 9, "unique_agents": 9}) == "LOW-ACTIVITY"
    assert observatory.classify_room({"messages": 10, "unique_agents": 8}) == "HIGH-CHURN"
    assert observatory.classify_room({"messages": 20, "unique_agents": 3}) == "COMMUNITY"
    assert observatory.classify_room({"messages": 20, "unique_agents": 4}) == "MIXED"


def test_json_report_counts_unmatched_room_lines():
    observatory = _observatory()

    report = observatory.build_json(
        [{"room": "lobby"}],
        ["/r/bad line"],
        [],
    )

    assert report["rooms_discovered"] == 1
    assert report["unmatched_room_lines"] == 1
