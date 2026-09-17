#!/usr/bin/env python3
"""Build a disposable, deterministic Technocore store for read benchmarks.

The fixture is written directly in the documented on-disk shape so setup never exercises
an HTTP write route. Point CHAT_ROOT at the printed root and start the ordinary app. Never
point this command at a real store: it refuses a non-empty destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _shard(name: str) -> str:
    return hashlib.blake2b(name.encode("utf-8"), digest_size=1).hexdigest()


def build(root: Path, rooms: int, messages: int, text_bytes: int) -> dict:
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"refusing non-empty fixture root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    text = "x" * text_bytes
    base = datetime(2026, 1, 1, tzinfo=UTC)
    specs = []
    seq_states: dict[str, dict] = {}
    total_payload = 0
    for room_index in range(rooms):
        room = f"bench-room-{room_index:03d}"
        shard = _shard(room)
        path = root / "rooms" / shard / f"{room}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            for seq in range(1, messages + 1):
                record = {
                    "seq": seq,
                    "ts": (base + timedelta(seconds=seq)).isoformat().replace("+00:00", "Z"),
                    "from": "bench",
                    "text": text,
                }
                line = json.dumps(record, separators=(",", ":")).encode() + b"\n"
                handle.write(line)
                total_payload += len(line)
        seq_states.setdefault(shard, {})[room] = {"floor": 0, "gen": 1, "t": 0}
        specs.append({"room": room, "since": 0, "limit": messages})

    for shard, state in seq_states.items():
        (root / f".seqstate.{shard}").write_text(
            json.dumps(state, separators=(",", ":")), encoding="utf-8"
        )
    rooms_file = root / "benchmark-rooms.json"
    rooms_file.write_text(json.dumps(specs, indent=2) + "\n", encoding="utf-8")
    return {
        "root": str(root.resolve()),
        "rooms_file": str(rooms_file.resolve()),
        "rooms": rooms,
        "messages_per_room": messages,
        "text_bytes": text_bytes,
        "stored_payload_bytes": total_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rooms", type=int, default=16)
    parser.add_argument("--messages", type=int, default=20)
    parser.add_argument("--text-bytes", type=int, default=128)
    args = parser.parse_args()
    if args.rooms < 1 or args.messages < 1 or args.text_bytes < 1:
        parser.error("rooms, messages and text-bytes must be positive")
    print(json.dumps(build(args.root, args.rooms, args.messages, args.text_bytes), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
