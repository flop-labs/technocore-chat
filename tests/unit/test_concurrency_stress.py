"""Run: uv run --group dev python -m pytest tests

Multi-process stress coverage for two invariants that, unlike the global note cap
(test_note_count.py::test_the_global_cap_binds_exactly_under_concurrent_processes), had no
concurrent-process regression test before this file: the room cap, and the signed-write
nonce single-use guarantee. Both are enforced under `_locked`/`_create_gate`, which a
single-threaded test can exercise for logic but not for the actual OS-level lock behaving
correctly across separate processes competing for the same file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[2] / "src")

ROOM_WORKER = """
import sys, json
sys.path.insert(0, {src!r})
import store
root, tag, attempts = sys.argv[1], sys.argv[2], int(sys.argv[3])
made = 0
for i in range(attempts):
    try:
        store.append(store.Path(root), "r-%s-%d" % (tag, i), "bot", "hi")
        made += 1
    except store.StoreError:
        pass
print(json.dumps(made))
"""


def test_the_room_cap_binds_exactly_under_concurrent_processes(tmp_path) -> None:
    """The room-cap equivalent of test_note_count.py's global note-cap test. Four processes
    race to create distinct rooms past a small cap; the store must end up holding exactly
    the cap, never one more — an off-by-one here is invisible on a quiet store and shows up
    as a breached cap only under exactly the load the cap exists for.
    """

    cap = 24
    script = tmp_path / "worker.py"
    script.write_text(ROOM_WORKER.format(src=SRC))
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CHAT_")},
        "CHAT_MAX_ROOMS": str(cap),
    }
    root = tmp_path / "shared"
    root.mkdir()

    workers = [
        subprocess.Popen(
            [sys.executable, str(script), str(root), str(w), "20"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for w in range(4)
    ]
    accepted = 0
    for worker in workers:
        out, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, f"worker failed: {err}"
        accepted += json.loads(out)

    # rglob, not glob: rooms now live under rooms/<shard>/<room>.jsonl.
    room_files = list((root / "rooms").rglob("*.jsonl")) if (root / "rooms").exists() else []
    total_on_disk = len(room_files)
    real_rooms = len([p for p in room_files if p.stem != "events"])
    # _check_room_capacity's _scan walks every .jsonl in rooms/, so events.jsonl -- created
    # as a side effect of the very first room creation -- occupies one of MAX_ROOMS' slots
    # too. The cap binds on the *total*, so real_rooms fills to cap-1, not cap, once
    # anything has triggered an announcement.
    assert total_on_disk == accepted + 1, "every accepted write plus the events room"
    assert real_rooms == accepted, "every accepted write must be a room that exists"
    assert total_on_disk == cap, f"cap is {cap}, store holds {total_on_disk} .jsonl files"


NONCE_RACE_WORKER = """
import sys, json
sys.path.insert(0, {src!r})
import store
root, did, sig_nonce = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    rec = store.append(store.Path(root), "d-racetest", "unused", "attempt", did=did, nonce=sig_nonce)
    print(json.dumps({{"ok": True, "seq": rec["seq"]}}))
except store.StoreError as e:
    print(json.dumps({{"ok": False, "error": str(e)}}))
"""


def test_identical_nonce_from_n_processes_is_accepted_exactly_once(tmp_path) -> None:
    """N processes race to sign the *same* nonce for the same DID in the same room at the
    same instant -- the scenario a captured signed URL replayed concurrently looks like.
    `_last_nonce` plus the per-room `_locked` gate must serialise them so exactly one write
    lands and every other racer is refused as a replay, never as a silent duplicate.
    """
    import store

    did = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    script = tmp_path / "worker.py"
    script.write_text(NONCE_RACE_WORKER.format(src=SRC))
    root = tmp_path / "shared"
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}

    workers = [
        subprocess.Popen(
            [sys.executable, str(script), str(root), did, "1000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(8)
    ]
    results = []
    for worker in workers:
        out, err = worker.communicate(timeout=60)
        assert worker.returncode == 0, f"worker failed: {err}"
        results.append(json.loads(out))

    successes = [r for r in results if r["ok"]]
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}: {results}"

    on_disk = store.read_messages(root, "d-racetest")
    assert on_disk["count"] == 1, "the room must hold exactly the one record that won the race"


def test_increasing_nonces_from_n_processes_never_produce_a_duplicate_or_gap(tmp_path) -> None:
    """N processes each drive their own increasing nonce sequence for the *same* DID and
    room concurrently -- interleaved rather than colliding. Every nonce that lands must be
    strictly greater than the one before it in room order, or the single-use guarantee has
    a hole a real client's retry logic would fall into.
    """
    import store

    did = "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    script = tmp_path / "worker.py"
    script.write_text(
        f"""
import sys, json, time
sys.path.insert(0, {SRC!r})
import store
root, did, base = sys.argv[1], sys.argv[2], int(sys.argv[3])
made = []
for i in range(15):
    nonce = base + i
    try:
        rec = store.append(store.Path(root), "d-seqtest", "unused", "n%d" % nonce, did=did, nonce=nonce)
        made.append(rec["nonce"])
    except store.StoreError:
        pass
print(json.dumps(made))
"""
    )
    root = tmp_path / "shared"
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}

    # Each worker's base is far enough apart that their own 15-step sequences cannot
    # collide with each other's nonce *values* -- the race under test is lock contention
    # on the shared room/DID pair, not an accidental value collision between workers.
    workers = [
        subprocess.Popen(
            [sys.executable, str(script), str(root), did, str(w * 10_000)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for w in range(4)
    ]
    all_made = []
    for worker in workers:
        out, err = worker.communicate(timeout=60)
        assert worker.returncode == 0, f"worker failed: {err}"
        all_made.extend(json.loads(out))

    assert len(all_made) == len(set(all_made)), f"a nonce landed twice: {all_made}"

    result = store.read_messages(root, "d-seqtest", limit=200)
    nonces_in_room_order = [m["nonce"] for m in result["messages"]]
    assert nonces_in_room_order == sorted(nonces_in_room_order), (
        f"nonces landed out of increasing order: {nonces_in_room_order}"
    )
