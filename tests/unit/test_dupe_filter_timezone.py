"""The offline room benchmark must treat stored Z timestamps as UTC."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from bench import dupe_filter


@pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="requires POSIX timezone conversion"
)
def test_room_analysis_keeps_two_second_window_across_local_dst(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The store emits UTC Z times. Parsing them as local time creates an artificial
    # one-hour gap across a host's fall-back transition, hiding real duplicates.
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0/2,M11.1.0/2")
    time.tzset()
    try:
        path = tmp_path / "room.jsonl"
        stamps = ["2026-11-01T01:59:59.000000Z"] * 5 + ["2026-11-01T02:00:01.000000Z"]
        text = "a repeated room message longer than sixteen characters"
        path.write_text(
            "".join(json.dumps({"ts": stamp, "text": text}) + "\n" for stamp in stamps),
            encoding="utf-8",
        )

        dupe_filter.analyse_room_file(str(path), min_length=16, max_copies=5, window=60)

        output = capsys.readouterr().out
        assert "6 records, 1 distinct normalised texts" in output
        # Five copies are allowed; the sixth is only two seconds later in UTC.
        # A local-time interpretation incorrectly treats it as 3602 seconds later.
        assert "1 of 6 refused" in output
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        time.tzset()
