"""The offline duplicate-filter benchmark must tolerate non-record JSONL lines."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# The CLI pytest entry point does not put the repository root on sys.path.
# Load the standalone benchmark by path, as the other benchmark tests do.
spec = importlib.util.spec_from_file_location(
    "dupe_filter_bench", Path(__file__).resolve().parents[2] / "bench" / "dupe_filter.py"
)
assert spec is not None and spec.loader is not None
dupe_filter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dupe_filter)


def test_room_analysis_skips_json_values_that_are_not_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    room = tmp_path / "room.jsonl"
    room.write_text(
        'null\n0\n[]\n"not a record"\n{"text": 7}\n'
        '{"text": "a genuine room line"}\n',
        encoding="utf-8",
    )

    dupe_filter.analyse_room_file(str(room), *dupe_filter.CHOSEN)

    output = capsys.readouterr().out
    assert "1 records, 1 distinct normalised texts" in output
    assert "0 of 1 refused" in output
