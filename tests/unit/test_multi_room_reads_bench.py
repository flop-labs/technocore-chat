from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx2 as httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.multi_room_fixture import build  # noqa: E402
from bench.multi_room_reads import RoomSpec, fetch_room, read_parallel  # noqa: E402
from bench.summarize_multi_room_reads import summarize  # noqa: E402


def _response(request: httpx.Request, room: str, first: int = 1) -> httpx.Response:
    body = {
        "room": room,
        "count": 1,
        "first_seq": first,
        "last_seq": first,
        "generation": 2,
        "messages": [{"seq": first, "text": "x"}],
    }
    return httpx.Response(200, json=body, request=request)


def test_fetch_preserves_cursor_generation_and_reports_gap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["since"] == "2"
        return _response(request, "alpha", 5)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_room(client, "http://test", RoomSpec("alpha", 2, 10), 1)
    assert result["status"] == "ok"
    assert result["generation"] == 2
    assert result["cursor"] == 2
    assert result["possible_retention_or_limit_gap"] is True


def test_parallel_keeps_room_errors_isolated_and_never_retries_429() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        room = request.url.path.rsplit("/", 1)[-1]
        calls.append(room)
        if room == "limited":
            return httpx.Response(429, headers={"retry-after": "9"}, request=request)
        return _response(request, room)

    specs = [RoomSpec("ok", 0, 1), RoomSpec("limited", 0, 1)]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = read_parallel(client, "http://test", specs, 1, 2)
    assert [result["status"] for result in results] == ["ok", "rate_limited"]
    assert sorted(calls) == ["limited", "ok"]


def test_fixture_refuses_non_empty_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    (root / "owned").write_text("leave me", encoding="utf-8")
    try:
        build(root, 1, 1, 8)
    except ValueError as exc:
        assert "refusing non-empty" in str(exc)
    else:
        raise AssertionError("non-empty root accepted")


def test_fixture_has_actual_room_and_generation_shape(tmp_path: Path) -> None:
    metadata = build(tmp_path / "store", 2, 3, 8)
    specs = json.loads(Path(metadata["rooms_file"]).read_text(encoding="utf-8"))
    assert len(specs) == 2
    assert len(list((tmp_path / "store" / "rooms").glob("*/*.jsonl"))) == 2
    assert len(list((tmp_path / "store").glob(".seqstate.*"))) == 2


def test_summary_reports_speedup_without_request_reduction() -> None:
    metadata = {"kind": "metadata", "room_count": 2}
    rows = [metadata]
    for method, times in (("sequential", [0.4, 0.5]), ("bounded_parallel", [0.2, 0.25])):
        for value in times:
            rows.append(
                {
                    "kind": "sample",
                    "method": method,
                    "elapsed_seconds": value,
                    "client_cpu_seconds": 0.01,
                    "client_rss_bytes_after": None,
                    "request_count": 2,
                    "response_body_bytes": 100,
                    "error_count": 0,
                    "possible_gap_count": 0,
                    "dataset_digest": "same",
                }
            )
    result = summarize(rows)
    assert result["comparison"]["parallel_median_speedup"] == 2
    assert result["results"]["sequential"]["latency_min_seconds"] == 0.4
    assert result["results"]["bounded_parallel"]["latency_max_seconds"] == 0.25
    assert result["results"]["sequential"]["latency_stdev_seconds"] > 0
    assert result["comparison"]["request_reduction"] == 0
    assert result["comparison"]["batch_endpoint_measured"] is False
