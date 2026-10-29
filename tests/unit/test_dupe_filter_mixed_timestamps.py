"""Mixed room timestamps must not create false duplicate refusals."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from bench import dupe_filter


def _room(path: Path, records: list[dict[str, str]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_room_analysis_skips_invalid_timestamps_in_mixed_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A malformed final timestamp cannot be placed on the synthetic 1000s axis
    # after valid 2026 epochs: that backwards jump makes old copies look live.
    phrase = "a genuine repeated room message"
    records = [
        {"text": phrase, "ts": f"2026-10-28T00:00:{i:02d}.000000Z"}
        for i in range(5)
    ]
    records += [
        {"text": "ok", "ts": "2026-10-28T02:00:00.000000Z"},
        {"text": phrase, "ts": "not-a-timestamp"},
    ]
    path = tmp_path / "mixed.jsonl"
    _room(path, records)

    dupe_filter.analyse_room_file(str(path), min_length=16, max_copies=5, window=60)
    output = capsys.readouterr().out

    assert "skipped 1 records with missing or invalid timestamps" in output
    assert "6 records" in output
    assert "0 of 6 refused" in output


def test_room_analysis_preserves_synthetic_only_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    phrase = "a genuine repeated room message"
    path = tmp_path / "no-timestamps.jsonl"
    _room(path, [{"text": phrase} for _ in range(6)])

    dupe_filter.analyse_room_file(str(path), min_length=16, max_copies=5, window=120)
    output = capsys.readouterr().out

    assert "1 of 6 refused" in output
    assert "skipped" not in output


def test_room_analysis_preserves_valid_timestamps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    phrase = "a genuine repeated room message"
    path = tmp_path / "valid.jsonl"
    _room(
        path,
        [
            {"text": phrase, "ts": f"2026-10-28T00:00:{i:02d}.000000Z"}
            for i in range(6)
        ],
    )

    dupe_filter.analyse_room_file(str(path), min_length=16, max_copies=5, window=60)
    output = capsys.readouterr().out

    assert "1 of 6 refused" in output
    assert "skipped" not in output


@pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="requires POSIX timezone conversion"
)
def test_room_analysis_uses_utc_for_dst_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Z is UTC, even when the host's local DST clock falls back between records.
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0/2,M11.1.0/2")
    time.tzset()
    try:
        phrase = "a genuine repeated room message"
        stamps = ["2026-11-01T01:59:59.000000Z"] * 5 + [
            "2026-11-01T02:00:01.000000Z"
        ]
        path = tmp_path / "dst.jsonl"
        _room(path, [{"text": phrase, "ts": stamp} for stamp in stamps])

        dupe_filter.analyse_room_file(str(path), min_length=16, max_copies=5, window=60)
        output = capsys.readouterr().out
        assert "6 records" in output
        # A naive local conversion sees 3602s, but the UTC gap is only 2s.
        assert "1 of 6 refused" in output
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        time.tzset()
