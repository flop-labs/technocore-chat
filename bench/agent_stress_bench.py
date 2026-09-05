#!/usr/bin/env python3
"""
High-Throughput Agent Stress & Latency Benchmark Suite
======================================================
Measures end-to-end cryptographic throughput, memory footprint,
concurrency scaling, and latency percentiles (p50, p90, p95, p99)
for autonomous agents communicating over the Technocore protocol.

Usage:
  python3 bench/agent_stress_bench.py --iterations 1000 --concurrency 20
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import statistics
import time
from dataclasses import dataclass

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

BASE58BTC_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTICODEC_ED25519 = b"\xed\x01"


def base58_encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    encoded = ""
    while num:
        num, rem = divmod(num, 58)
        encoded = BASE58BTC_ALPHABET[rem] + encoded
    return "1" * zeroes + encoded


@dataclass
class BenchmarkResult:
    name: str
    total_operations: int
    duration_sec: float
    throughput_ops_sec: float
    latency_min_ms: float
    latency_p50_ms: float
    latency_p90_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float


def benchmark_key_generation(iterations: int) -> BenchmarkResult:
    """Benchmark Ed25519 key generation and did:key multicodec derivation."""
    latencies: list[float] = []
    t0 = time.perf_counter()

    for _ in range(iterations):
        op_start = time.perf_counter()
        key = Ed25519PrivateKey.generate()
        raw_pub = key.public_key().public_bytes_raw()
        _did = "did:key:z" + base58_encode(MULTICODEC_ED25519 + raw_pub)
        op_end = time.perf_counter()
        latencies.append((op_end - op_start) * 1000.0)

    total_time = time.perf_counter() - t0
    latencies.sort()

    return BenchmarkResult(
        name="Ed25519 KeyGen + DID Derivation",
        total_operations=iterations,
        duration_sec=total_time,
        throughput_ops_sec=iterations / total_time,
        latency_min_ms=latencies[0],
        latency_p50_ms=statistics.median(latencies),
        latency_p90_ms=latencies[int(len(latencies) * 0.90)],
        latency_p95_ms=latencies[int(len(latencies) * 0.95)],
        latency_p99_ms=latencies[int(len(latencies) * 0.99)],
        latency_max_ms=latencies[-1],
    )


def benchmark_payload_signing(iterations: int, concurrency: int = 1) -> BenchmarkResult:
    """Benchmark high-throughput monotonic payload cryptographic signing."""
    key = Ed25519PrivateKey.generate()
    sample_payload = "technocore|1725255600000000000|Agent high-throughput cryptographic verification benchmark"

    def sign_worker(n_ops: int) -> list[float]:
        lats = []
        for _ in range(n_ops):
            t_s = time.perf_counter()
            sig = key.sign(sample_payload.encode("utf-8"))
            _ = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
            t_e = time.perf_counter()
            lats.append((t_e - t_s) * 1000.0)
        return lats

    t0 = time.perf_counter()
    all_latencies: list[float] = []

    if concurrency <= 1:
        all_latencies = sign_worker(iterations)
    else:
        ops_per_worker = iterations // concurrency
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(sign_worker, ops_per_worker) for _ in range(concurrency)]
            for fut in concurrent.futures.as_completed(futures):
                all_latencies.extend(fut.result())

    total_time = time.perf_counter() - t0
    all_latencies.sort()

    return BenchmarkResult(
        name=f"Ed25519 Payload Signing ({concurrency} threads)",
        total_operations=len(all_latencies),
        duration_sec=total_time,
        throughput_ops_sec=len(all_latencies) / total_time,
        latency_min_ms=all_latencies[0],
        latency_p50_ms=statistics.median(all_latencies),
        latency_p90_ms=all_latencies[int(len(all_latencies) * 0.90)],
        latency_p95_ms=all_latencies[int(len(all_latencies) * 0.95)],
        latency_p99_ms=all_latencies[int(len(all_latencies) * 0.99)],
        latency_max_ms=all_latencies[-1],
    )


def print_report(results: list[BenchmarkResult]) -> None:
    """Print formatted markdown & terminal table of benchmark results."""
    print("\n" + "=" * 95)
    print(" 🚀 TECHNOCORE AGENT HIGH-THROUGHPUT PERFORMANCE BENCHMARK REPORT")
    print("=" * 95)
    header = (
        f"{'Benchmark Target':<35} | {'Ops':<7} | {'Throughput (ops/s)':<18} | "
        f"{'p50 (ms)':<9} | {'p95 (ms)':<9} | {'p99 (ms)':<9}"
    )
    print(header)
    print("-" * 95)
    for r in results:
        row = (
            f"{r.name:<35} | {r.total_operations:<7} | {r.throughput_ops_sec:>18.1f} | "
            f"{r.latency_p50_ms:>9.4f} | {r.latency_p95_ms:>9.4f} | {r.latency_p99_ms:>9.4f}"
        )
        print(row)
    print("=" * 95)


def main() -> None:
    parser = argparse.ArgumentParser(description="Technocore Agent Benchmark Suite")
    parser.add_argument("--iterations", type=int, default=5000, help="Number of operations per benchmark")
    parser.add_argument("--concurrency", type=int, default=8, help="Worker concurrency level")
    args = parser.parse_args()

    if not HAS_CRYPTO:
        print("[-] Error: 'cryptography' library is required to run cryptographic benchmarks.")
        return

    print(f"[*] Initializing benchmarks (Iterations: {args.iterations}, Concurrency: {args.concurrency})...")
    results = [
        benchmark_key_generation(iterations=args.iterations // 2),
        benchmark_payload_signing(iterations=args.iterations, concurrency=1),
        benchmark_payload_signing(iterations=args.iterations, concurrency=args.concurrency),
    ]
    print_report(results)


if __name__ == "__main__":
    main()
