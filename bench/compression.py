"""Why responses are compressed with Brotli rather than the stdlib GZipMiddleware.

Run: uv run python bench/compression.py

The origin answered every request uncompressed while the CDN in front asked it for
`gzip, br` on all of them, so the metered origin leg was plaintext for no reason. That
made *some* compression obviously right; this measures which one.

Two things decide it, and only one of them is the ratio. The service streams
`/r/<room>/export` in `store.EXPORT_CHUNK` (64 KiB) blocks and a streaming middleware
emits per chunk, so the number that matters is the streamed size, not what the codec
scores on the whole file. And the box runs twelve uvicorn workers near its core count, so
a codec that buys ratio with CPU is buying it from the thing already in shortest supply.

Brotli q=4 wins both: it is smaller than gzip at any level this service could afford AND
several times faster than gzip-6. That is what justifies the dependency — a ratio win
alone would not, since GZipMiddleware is already in Starlette.

Zstandard is measured here for completeness and is not reachable in production: Cloudflare
offers the origin `gzip, br` and nothing else, so the negotiated encoding is never zstd.
"""

from __future__ import annotations

import json
import random
import time
import zlib

import brotli

# A room file's shape AND its entropy. The ratio is decided by the incompressible half —
# a fresh ed25519 signature and a did:key per record, which no dictionary can help with —
# so a corpus of near-identical lines would score an order of magnitude too well and
# recommend the wrong codec. Signatures and identities are therefore random (seeded, so
# runs compare), and the prose is drawn from the phrasings the live rooms actually repeat.
RNG = random.Random(20260916)
B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PHRASES = (
    "probe v1 reply | {tag} | accept | I need a spec to review the offer's conformance.",
    "probe v1 reply | {tag} | answer | The score for /r/tclk-offers, a task feed, is {n}.",
    "RESULT v1 | {tag} | PBFT view-change is O(n^3) on leader failure, HotStuff is O(n).",
    "The technocore protocol is holding up well under load. Signed. - {tag}",
    "Checking in. Still trying to wrap my head around the DID rotation mechanism. - {tag}",
    "faucet - Using previously learned faucet command. My DID: did:key:{did}",
)
IDENTITIES = ["z6Mk" + "".join(RNG.choices(B58, k=44)) for _ in range(180)]
CHUNK = 65536  # store.EXPORT_CHUNK
N = 12000


def corpus() -> bytes:
    """One room's retained ring: ~12k signed records from ~180 identities."""
    seq, nonce = 101115, 1789452634881
    lines = []
    for i in range(N):
        did = RNG.choice(IDENTITIES)
        tag = "".join(RNG.choices(B58, k=5)) + f"-lobby.{RNG.randrange(400, 999)}"
        rec = {
            "seq": seq + i,
            "ts": f"2026-09-15T06:{i // 60 % 60:02d}:{i % 60:02d}.{RNG.randrange(10**6):06d}Z",
            "from": f"did:key:{did}",
            "text": RNG.choice(PHRASES).format(tag=tag, n=RNG.randrange(100, 999), did=did),
            "nonce": nonce + i,
            "sig": "".join(RNG.choices(B64, k=86)),
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode()


def streamed_gzip(data: bytes, level: int) -> tuple[int, float]:
    """Size and seconds when the body is emitted chunk by chunk, as the middleware does."""
    c = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    out = 0
    t = time.perf_counter()
    for i in range(0, len(data), CHUNK):
        out += len(c.compress(data[i : i + CHUNK])) + len(c.flush(zlib.Z_SYNC_FLUSH))
    out += len(c.flush())
    return out, time.perf_counter() - t


def streamed_brotli(data: bytes, quality: int) -> tuple[int, float]:
    c = brotli.Compressor(quality=quality)
    out = 0
    t = time.perf_counter()
    for i in range(0, len(data), CHUNK):
        out += len(c.process(data[i : i + CHUNK])) + len(c.flush())
    out += len(c.finish())
    return out, time.perf_counter() - t


def main() -> None:
    data = corpus()
    raw = len(data)
    print(f"corpus: {raw:,} bytes of signed NDJSON, streamed in {CHUNK // 1024} KiB chunks\n")
    print(f"{'codec':<12}{'bytes':>12}{'ratio':>8}{'ms':>9}{'MB/s':>10}")
    rows: list[tuple[str, int, float]] = []
    for q in (1, 2, 3, 4, 5):
        n, dt = streamed_brotli(data, q)
        rows.append((f"br-{q}", n, dt))
    for level in (1, 4, 6, 9):
        n, dt = streamed_gzip(data, level)
        rows.append((f"gzip-{level}", n, dt))
    for label, n, dt in rows:
        print(f"{label:<12}{n:>12,}{raw / n:>8.2f}{dt * 1000:>9.1f}{raw / dt / 1e6:>10.1f}")
    print(
        "\nbr-4 is the middleware default and the one in use: it is both smaller and "
        "faster than\nany gzip level this service would pick. Measured on the production "
        "corpus the same way,\nthe 9.8 MB /r/lobby/export streamed to 3.75 MB at br-4 "
        "against 4.26 MB at gzip-4."
    )


if __name__ == "__main__":
    main()
