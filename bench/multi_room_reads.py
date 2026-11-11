#!/usr/bin/env python3
"""Benchmark sequential versus bounded-parallel multi-room GETs.

This client never writes. Each room keeps its own cursor and generation, failures stay
room-local, 429 responses are recorded without retry, and every socket is closed at exit.
The responses are independent room snapshots; they are not an atomic multi-room snapshot.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2 as httpx

REQUIRED_FIELDS = {"room", "count", "first_seq", "last_seq", "generation", "messages"}


@dataclass(frozen=True)
class RoomSpec:
    room: str
    since: int | None
    limit: int


def _rss_bytes() -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    get_process_memory_info.restype = wintypes.BOOL
    ok = get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb)
    return int(counters.WorkingSetSize) if ok else None


def load_specs(path: Path) -> list[RoomSpec]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("rooms file must be a non-empty JSON array")
    specs = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each room entry must be an object")
        room, since, limit = item.get("room"), item.get("since"), item.get("limit", 50)
        if not isinstance(room, str) or not room:
            raise ValueError("room must be a non-empty string")
        if since is not None and (not isinstance(since, int) or since < 0):
            raise ValueError(f"invalid cursor for {room}")
        if not isinstance(limit, int) or limit < 1:
            raise ValueError(f"invalid limit for {room}")
        specs.append(RoomSpec(room, since, limit))
    if len({item.room for item in specs}) != len(specs):
        raise ValueError("room names must be unique")
    return specs


def fetch_room(client: httpx.Client, base_url: str, spec: RoomSpec, timeout: float) -> dict:
    params: dict[str, Any] = {"format": "json", "limit": spec.limit}
    if spec.since is not None:
        params["since"] = spec.since
    started = time.perf_counter()
    try:
        response = client.get(
            f"{base_url.rstrip('/')}/r/{quote(spec.room, safe='')}",
            params=params,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - started
        size = len(response.content)
        if response.status_code == 429:
            return {
                "room": spec.room,
                "status": "rate_limited",
                "http_status": 429,
                "elapsed_seconds": elapsed,
                "response_body_bytes": size,
                "retry_after": response.headers.get("retry-after"),
            }
        if response.status_code != 200:
            return {
                "room": spec.room,
                "status": "http_error",
                "http_status": response.status_code,
                "elapsed_seconds": elapsed,
                "response_body_bytes": size,
            }
        body = response.json()
        if not isinstance(body, dict) or not REQUIRED_FIELDS.issubset(body):
            raise ValueError("response does not match the published room-read shape")
        if body["room"] != spec.room or not isinstance(body["messages"], list):
            raise ValueError("response room/messages mismatch")
        if body["count"] != len(body["messages"]):
            raise ValueError("response count does not match messages")
        first_seq = body["first_seq"]
        truncated = bool(
            spec.since is not None
            and first_seq is not None
            and isinstance(first_seq, int)
            and first_seq > spec.since + 1
        )
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "room": spec.room,
            "status": "ok",
            "http_status": 200,
            "elapsed_seconds": elapsed,
            "response_body_bytes": size,
            "count": body["count"],
            "first_seq": first_seq,
            "last_seq": body["last_seq"],
            "generation": body["generation"],
            "cursor": spec.since,
            "possible_retention_or_limit_gap": truncated,
            "response_digest": digest,
        }
    except Exception as exc:  # room-local by design; the remaining rooms still run
        return {
            "room": spec.room,
            "status": "client_error",
            "error_type": type(exc).__name__,
            "elapsed_seconds": time.perf_counter() - started,
            "response_body_bytes": 0,
        }


def read_sequential(
    client: httpx.Client, base_url: str, specs: list[RoomSpec], timeout: float
) -> list[dict]:
    return [fetch_room(client, base_url, spec, timeout) for spec in specs]


def read_parallel(
    client: httpx.Client,
    base_url: str,
    specs: list[RoomSpec],
    timeout: float,
    concurrency: int,
) -> list[dict]:
    results: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(fetch_room, client, base_url, spec, timeout): spec.room for spec in specs
        }
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()
    return [results[spec.room] for spec in specs]


def _snapshot_digest(results: list[dict]) -> str | None:
    if any(result["status"] != "ok" for result in results):
        return None
    values = [(result["room"], result["response_digest"]) for result in results]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _sample(method: str, concurrency: int, results: list[dict], wall: float, cpu: float) -> dict:
    errors = sum(result["status"] != "ok" for result in results)
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "method": method,
        "concurrency": concurrency,
        "elapsed_seconds": wall,
        "client_cpu_seconds": cpu,
        "client_rss_bytes_after": _rss_bytes(),
        "request_count": len(results),
        "response_body_bytes": sum(result["response_body_bytes"] for result in results),
        "returned_messages": sum(result.get("count", 0) for result in results),
        "error_count": errors,
        "error_rate": errors / len(results),
        "possible_gap_count": sum(
            bool(result.get("possible_retention_or_limit_gap")) for result in results
        ),
        "dataset_digest": _snapshot_digest(results),
        "room_results": results,
    }


def run_arm(
    method: str,
    client: httpx.Client,
    base_url: str,
    specs: list[RoomSpec],
    timeout: float,
    concurrency: int,
) -> dict:
    cpu_started = time.process_time()
    started = time.perf_counter()
    if method == "sequential":
        results = read_sequential(client, base_url, specs, timeout)
        workers = 1
    else:
        results = read_parallel(client, base_url, specs, timeout, concurrency)
        workers = concurrency
    return _sample(
        method,
        workers,
        results,
        time.perf_counter() - started,
        time.process_time() - cpu_started,
    )


def _client(concurrency: int) -> httpx.Client:
    limits = httpx.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency, keepalive_expiry=30
    )
    return httpx.Client(limits=limits, http2=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--rooms-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--delay-ms", type=float, required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--fixture-description", default="not supplied")
    parser.add_argument("--environment-note", default="")
    args = parser.parse_args()
    if args.concurrency < 1 or args.timeout <= 0 or args.repetitions < 1 or args.warmups < 0:
        parser.error("invalid concurrency, timeout, repetitions or warmups")
    specs = load_specs(args.rooms_file)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "kind": "metadata",
        "schema": 1,
        "upstream_commit": args.upstream_commit,
        "fixture_description": args.fixture_description,
        "environment_note": args.environment_note,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "room_count": len(specs),
        "limits": sorted({spec.limit for spec in specs}),
        "delay_ms": args.delay_ms,
        "delay_injection": "ASGI wrapper before each GET /r/* request",
        "connection_reuse": True,
        "parallel_concurrency": args.concurrency,
        "warmups": args.warmups,
        "repetitions_per_method": args.repetitions,
        "server_workers": 1,
        "server_cpu": "NOT_MEASURED",
        "server_memory": "NOT_MEASURED",
        "transport_header_bytes": "NOT_MEASURED",
        "snapshot_semantics": "independent per-room snapshots; not atomic across rooms",
    }
    rows: list[dict] = [metadata]
    with _client(1) as sequential_client, _client(args.concurrency) as parallel_client:
        for _ in range(args.warmups):
            run_arm("sequential", sequential_client, args.base_url, specs, args.timeout, 1)
            run_arm(
                "bounded_parallel",
                parallel_client,
                args.base_url,
                specs,
                args.timeout,
                args.concurrency,
            )
        for repetition in range(args.repetitions):
            order = ("sequential", "bounded_parallel")
            if repetition % 2:
                order = tuple(reversed(order))
            for method in order:
                client = sequential_client if method == "sequential" else parallel_client
                row = run_arm(method, client, args.base_url, specs, args.timeout, args.concurrency)
                row.update({"kind": "sample", "repetition": repetition})
                rows.append(row)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    if any(row.get("error_count", 0) for row in rows if row["kind"] == "sample"):
        return 2
    digests = {row["dataset_digest"] for row in rows if row["kind"] == "sample"}
    return 0 if len(digests) == 1 and None not in digests else 3


if __name__ == "__main__":
    raise SystemExit(main())
