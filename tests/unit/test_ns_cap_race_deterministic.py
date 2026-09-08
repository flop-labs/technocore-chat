"""Deterministic repro: the per-namespace note cap (MAX_NOTES_PER_NS) can be silently
exceeded, with no real threads -- the exact interleaving is forced directly, the way
_race_before_lock forces a lock-boundary interleaving elsewhere in this test suite.

_create_gate's `check()` runs twice: once unlocked, before anything is reserved, and once
inside the gate's own lock, authoritatively (store.py's _create_gate docstring). The unlocked
call exists only to refuse a request before it costs an inode -- its own docstring says
"It reads counters and never persists a zero, so it creates nothing itself."

_check_note_capacity is the `check` callback for BOTH calls (note_set passes the same lambda
to _create_gate for both), and it always calls _note_totals(ns_dir, _ns_totals, persist=True).
_note_totals only persists when its read of the sidecar file (.notes-count) fails and it has
to rebuild by walking -- and it does that rebuild-and-persist regardless of which of the two
`check()` calls triggered it. So the UNLOCKED early call can persist a rebuilt count too,
contradicting _note_totals' own docstring ("persist ... is only turned on [by] the one [that]
runs inside the create gate, which IS this file's lock").

If that unlocked persist lands after a concurrent, real, lock-protected reservation has
already bumped the authoritative count, it overwrites the correct number with a stale, lower
one -- and every later create is then checked against a count that undercounts what is really
on disk, letting the namespace grow past MAX_NOTES_PER_NS with no cap enforcing it again until
a reap physically removes entries.
"""

import config
import store


def test_unlocked_early_check_can_clobber_a_concurrent_reservation(tmp_path, monkeypatch):
    root = tmp_path
    ns = "racens"
    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 3)
    monkeypatch.setattr(config, "MAX_NOTES_PER_NS", 3)

    # Two legitimate notes, both through the real path: .notes-count is correctly 2, and
    # ns_dir/.notes-count exists on disk (not missing, not corrupt).
    store.note_set(root, ns, "k1", "v1")
    store.note_set(root, ns, "k2", "v2")
    ns_dir = store._note_ns_dir(root, ns)
    count_file = ns_dir / store.NOTES_FILE
    assert count_file.read_text().split()[0] == "2"

    # Simulate the sidecar becoming untrustworthy while the two note files it counts stay on
    # disk -- store.py's own docstring for _note_totals treats "the file cannot be parsed" as
    # a real, expected state ("a file written by an older build parses as untrusted and
    # rebuilds by walking"); deleting it reproduces exactly that state without inventing a new
    # one, and forces the next _note_totals(ns_dir, ...) to fall into its rebuild-and-persist
    # branch, which is the branch under test.
    count_file.unlink()

    real_ns_totals = store._ns_totals
    fired = []

    def racing_rebuild(d):
        # This stands in for the early, UNLOCKED check()'s walk landing first in wall-clock
        # time. Capture what's really on disk right now (k1, k2 only: totals == (2, ...)).
        stale_totals = real_ns_totals(d)
        if not fired:
            fired.append(True)
            # Now let a fully independent, legitimate create win the race and land for real,
            # through the real, correctly-locked path -- exactly like a second in-flight
            # request's reservation completing while this one's early check is still in
            # flight. This performs its own real gate/lock/reservation/write; nothing here
            # bypasses that machinery.
            store.note_set(root, ns, "k3", "v3")
            assert count_file.read_text().split()[0] == "3", (
                "the concurrent create's own locked reservation should have landed 3"
            )
        # Return the walk captured BEFORE k3 landed -- the stale view the early check actually
        # had at the moment it looked.
        return stale_totals

    monkeypatch.setattr(store, "_ns_totals", racing_rebuild)

    # A fourth, distinct key. Its early, unlocked check() call reaches _note_totals, which
    # cannot read the now-missing sidecar, so it rebuilds via the monkeypatched walk above --
    # which lets k3 land for real in the middle of it. On the fixed code this rebuild is never
    # persisted off the lock, so k4 correctly sees the true, post-k3 count and is refused
    # (the namespace is already at cap after k1/k2/k3); on the buggy code the stale pre-k3
    # total gets persisted, k4 wrongly slips through, and the cap is exceeded on disk. Either
    # outcome for THIS call is acceptable here -- what must never happen is the cap being
    # silently exceeded, checked below regardless of which way k4 went.
    try:
        store.note_set(root, ns, "k4", "v4")
    except store.StoreError:
        pass  # correctly refused: the namespace was already full after k1/k2/k3

    assert fired, "the racing rebuild never fired -- test setup didn't reach the target branch"

    on_disk = sum(1 for p in ns_dir.glob("**/*.txt"))
    counted = int(count_file.read_text().split()[0])

    assert on_disk <= store.MAX_NOTES_PER_NS, (
        f"MAX_NOTES_PER_NS={store.MAX_NOTES_PER_NS} was silently exceeded: {on_disk} notes "
        f"are really on disk in this namespace -- k4 was wrongly accepted (200, not 400) "
        f"when the true count (k1, k2, k3) already stood at the cap, because the unlocked "
        f"early check's persist clobbered the correctly-reserved count of 3 back down to 2"
    )
    assert counted >= on_disk, (
        f".notes-count says {counted} but {on_disk} notes are really on disk -- the "
        "unlocked early check's persist clobbered the correct, concurrently-reserved count, "
        "and every later create is now checked against a permanently undercounted total"
    )
