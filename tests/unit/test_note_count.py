"""Run: uv run --group dev python -m pytest tests

The global note cap used to be enforced by walking every namespace on every new note, so a
create cost O(all notes) while the notes were growing. `.notes-count` replaced that walk.
Two things have to hold, and the second is the one that would actually hurt if it broke:
the cost must stop scaling with the store, and the cap must still bind *exactly* — a cached
count that drifts low lets the cap be breached, which is worse than the walk it replaced.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")


def _scandir_calls(monkeypatch, work) -> int:
    """How many directories `work` reads. The unit that matters: the old code opened one
    per namespace, so this number grew with the store."""
    import store

    calls = 0
    real = os.scandir

    def counting(path):
        nonlocal calls
        calls += 1
        return real(path)

    monkeypatch.setattr(store.os, "scandir", counting)
    work()
    monkeypatch.setattr(store.os, "scandir", real)
    return calls


def _seed(root: Path, namespaces: int) -> None:
    import store

    for n in range(namespaces):
        store.note_set(root, f"ns{n}", "seed", "v")


@pytest.mark.parametrize("namespaces", [4, 60])
def test_a_new_note_reads_the_same_number_of_directories_at_any_store_size(
    tmp_path, monkeypatch, namespaces
):
    """Parametrised rather than looped so a failure names the size it failed at. The count
    must be identical for both, which is the whole claim — see the assertion below."""
    import store

    root = tmp_path / f"store{namespaces}"
    _seed(root, namespaces)
    (root / ".reaped").touch()  # reap is throttled; measure the create path, not a reap

    fresh = store.note_path(root, "ns0", "brand-new")
    reads = _scandir_calls(monkeypatch, lambda: store._check_note_capacity(root, fresh))
    (tmp_path / f"reads{namespaces}.txt").write_text(str(reads))
    # One directory: the caller's own namespace, for the per-namespace cap. The global cap
    # reads a file instead of walking. Two would already mean the global walk is back.
    assert reads == 1, f"{namespaces} namespaces cost {reads} directory reads, expected 1"


def test_the_count_survives_a_lost_file_by_walking(tmp_path) -> None:
    """The fallback is the safety property: anything wrong with the file must degrade to
    the old behaviour — the exact count, paid for by walking — and never to a wrong number.
    A create after the loss must also leave the file correct again."""
    import store

    _seed(tmp_path, 5)
    assert store._note_count(tmp_path) == 5

    (tmp_path / store.NOTES_FILE).unlink()
    assert store._note_count(tmp_path) == 5, "a missing count must be rebuilt by walking"

    (tmp_path / store.NOTES_FILE).write_text("not a number")
    assert store._note_count(tmp_path) == 5, "a malformed count must be rebuilt by walking"

    (tmp_path / store.NOTES_FILE).write_text("-3")
    assert store._note_count(tmp_path) == 5, "a negative count must be rebuilt by walking"

    store.note_set(tmp_path, "ns0", "another", "v")
    assert store._note_count(tmp_path) == 6
    assert int((tmp_path / store.NOTES_FILE).read_text()) == 6


def test_a_reap_reconciles_a_drifted_count(tmp_path, monkeypatch) -> None:
    """Drift is bounded by one reap interval rather than by hope. Writing a deliberately
    wrong count and running a reap must restore the truth — this is what keeps a lost
    increment (an unclean shutdown under CHAT_FSYNC=0) from being permanent."""
    import store

    _seed(tmp_path, 3)
    (tmp_path / store.NOTES_FILE).write_text("999")
    assert store._note_count(tmp_path) == 999, "premise: the bogus count is being read"

    monkeypatch.setattr(store, "REAP_EVERY", 0)  # due now, rather than in five minutes
    store._reap(tmp_path)
    assert store._note_count(tmp_path) == 3


# --------------------------------------------------------------------------- the cap

# A worker: create notes as fast as it can into one shared root, and report how many the
# store accepted. Run as a separate *process* because that is the thing being tested —
# production runs `uvicorn --workers 3`, so the gate has to hold across processes, and
# threads in one interpreter would not exercise the flock at all.
WORKER = """
import sys, json
sys.path.insert(0, {src!r})
import store
root, tag, attempts = sys.argv[1], sys.argv[2], int(sys.argv[3])
made = 0
for i in range(attempts):
    try:
        store.note_set(store.Path(root), "ns-%s-%d" % (tag, i), "k", "v")
        made += 1
    except store.StoreError:
        pass
print(json.dumps(made))
"""


def test_the_global_cap_binds_exactly_under_concurrent_processes(tmp_path) -> None:
    """The regression that would actually hurt. Four processes race to create past a small
    cap; the store must end up holding exactly the cap, never one more.

    One namespace per note, so the *global* cap is the one under test — MAX_NOTES_PER_NS is
    MAX_ROOMS, so workers sharing a namespace hit the per-namespace cap first and the global
    one is never reached.

    An off-by-one here is invisible on a quiet store and shows up as a breached cap under
    exactly the load the cap exists for, so it is worth the process spawns.
    """
    import store

    cap = 24
    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(src=SRC))
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CHAT_")},
        "CHAT_MAX_ROOMS": str(cap // 8),  # MAX_NOTES_TOTAL = 8 * MAX_ROOMS
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

    on_disk = store._count_notes(root)
    assert on_disk == accepted, "every accepted write must be a note that exists"
    assert on_disk == cap, f"cap is {cap}, store holds {on_disk}"
    # …and the file agrees with the disk, or the next process starts from a wrong number.
    assert store._note_count(root) == cap
