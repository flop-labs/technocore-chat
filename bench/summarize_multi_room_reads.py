#!/usr/bin/env python3
"""Aggregate raw JSONL emitted by bench/multi_room_reads.py."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summarize(rows: list[dict]) -> dict:
    metadata = next(row for row in rows if row.get("kind") == "metadata")
    samples = [row for row in rows if row.get("kind") == "sample"]
    grouped: dict[str, dict[str, Any]] = {}
    for method in ("sequential", "bounded_parallel"):
        arm = [row for row in samples if row["method"] == method]
        times = [row["elapsed_seconds"] for row in arm]
        grouped[method] = {
            "samples": len(arm),
            "latency_median_seconds": statistics.median(times),
            "latency_p95_seconds": percentile(times, 0.95),
            "latency_min_seconds": min(times),
            "latency_max_seconds": max(times),
            "latency_stdev_seconds": statistics.stdev(times) if len(times) > 1 else 0.0,
            "client_cpu_median_seconds": statistics.median(
                row["client_cpu_seconds"] for row in arm
            ),
            "client_rss_median_bytes_after": statistics.median(
                row["client_rss_bytes_after"]
                for row in arm
                if row["client_rss_bytes_after"] is not None
            )
            if any(row["client_rss_bytes_after"] is not None for row in arm)
            else "NOT_MEASURED",
            "requests_per_sample": sorted({row["request_count"] for row in arm}),
            "response_body_bytes_per_sample": sorted({row["response_body_bytes"] for row in arm}),
            "errors": sum(row["error_count"] for row in arm),
            "possible_gaps": sum(row["possible_gap_count"] for row in arm),
        }
    seq = grouped["sequential"]
    parallel = grouped["bounded_parallel"]
    seq_median = float(seq["latency_median_seconds"])
    parallel_median = float(parallel["latency_median_seconds"])
    seq_p95 = float(seq["latency_p95_seconds"])
    parallel_p95 = float(parallel["latency_p95_seconds"])
    digests = {row["dataset_digest"] for row in samples}
    return {
        "metadata": metadata,
        "results": grouped,
        "comparison": {
            "same_dataset_all_samples": len(digests) == 1 and None not in digests,
            "parallel_median_speedup": seq_median / parallel_median,
            "parallel_p95_speedup": seq_p95 / parallel_p95,
            "request_reduction": 0,
            "batch_endpoint_measured": False,
        },
        "limits": [
            "Application-layer delay is not a WAN/TLS/proxy measurement.",
            "Response body bytes exclude HTTP headers and framing.",
            "Server CPU, memory and thread occupancy were not measured.",
            "No batch endpoint was implemented or measured.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.raw.read_text(encoding="utf-8").splitlines()]
    result = summarize(rows)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
