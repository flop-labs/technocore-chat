"""Run: uv run --group dev python -m pytest tests

The #173 regression, kept in its own file deliberately: it is the acceptance test for a
race, not for an implementation. #179, #501 and #629 each propose a different mechanism
for closing it, so the test that decides whether any of them did belongs somewhere all
three can carry unchanged.

Filed originally against #628, marked a duplicate of #173 in triage on 2026-09-06.
"""

import subprocess
import sys
import textwrap

import _client
import pytest
from _client import _claim, _keypair, _race_before_lock

client = _client.client  # the shared TestClient fixture


def _plumbing(ok, what):
    """Fail the build rather than the marker.

    `pytest.fail` raises `Failed`, which is not an `AssertionError` and so falls outside
    the `raises=` on the test below: it is reported as a real FAILURE even from inside an
    xfail-marked test, where a bare `assert` would be swallowed as "expected". Everything
    that is not the #173 outcome itself goes through here, because a harness that quietly
    stopped working must never read as "the bug is still there".
    """
    if not ok:
        pytest.fail(what)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#173 is open: the owner note is written with no compare-and-set. strict, so "
    "this fails the build the moment it starts passing -- whoever lands the fix deletes "
    "this marker in the same change, and the suite refuses to let it be forgotten. "
    "raises, so only the defect's own AssertionError is expected: anything else -- a dead "
    "hook, a moved helper, a 500 -- is a failure, not a reproduction.",
)
def test_two_first_claims_with_distinct_nonces_cannot_both_own_a_room(
    client, tmp_path, monkeypatch
):
    """Two callers claiming one unowned room, each with its own nonce, must not both win.

    The existing pair in test_rooms.py races the *nonce counter*, and the nonce CAS is what
    catches them: both claimers spend nonce 1, so the second loses the counter and is
    refused before it ever reaches the owner note. Distinct nonces walk straight past that.
    Each claimer burns a counter value it is entitled to, both gates read the room as
    unowned, and the owner note is then written with neither `expect` nor `expect_absent` --
    so the second write is an unconditional overwrite and the room changes hands without
    its first owner doing anything wrong.

    The interleaving is forced at the owner note's own lock rather than left to timing: the
    losing claimer is between the gate read that saw "unowned" and the lock it is about to
    take, which is exactly where a server-side compare-and-set has to live to help.

    Marked strict-xfail rather than left red. A red test cannot merge, so it would sit in a
    branch and gate nothing; strict-xfail lands the bar on main today, keeps CI honest while
    #173 is open, and turns the fix into a build failure until the marker goes. Re-measured
    2026-09-12 on base main@20a4457, each branch at its own head: this xpasses on #629
    (22c7e43), so #629 settles the race; it still xfails on #501 (3209cb8); and #179
    (bcdfdf2) cannot be measured -- it conflicts with main in four files.

    The marker is scoped to `AssertionError` and everything upstream of the invariant goes
    through `_plumbing`, so the expected failure is the defect and nothing else. Unscoped,
    the marker would have reported a dead hook or a renamed helper as "#173 reproduced" --
    and then, once the race was actually closed, gone on reporting it, so the fix would
    never have tripped the strict xpass this file exists to trip.
    """
    import store

    first, _ = _keypair(seed=1)
    second, second_sign = _keypair(seed=2)
    owner_path = store.note_path(tmp_path, store.OWNERS_NS, "d-racetoc")

    def the_other_claim_lands():
        """The first claimer completes, in the gap. Written directly rather than through a
        second request: the hook fires inside the store call the losing request is already
        making, so re-entering the app here would nest that request inside itself."""
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text(first, encoding="utf-8")

    raced = _race_before_lock(monkeypatch, store, owner_path, the_other_claim_lands)
    lost = _claim(client, "d-racetoc", second, second_sign, nonce=2)

    _plumbing(bool(raced), "the race never happened -- this test proved nothing")
    _plumbing(
        lost.status_code < 500,
        f"the losing claim did not reach a decision ({lost.status_code}) -- the race was "
        "forced but the server errored, so nothing was proved either way",
    )

    # Past this line an AssertionError is #173 itself, which is what the marker expects.
    # The room keeps the owner that got there first. Which status the loser sees is the
    # implementation's call -- 409 and 403 are both defensible -- but it must not be 200,
    # and the stored owner is not negotiable.
    assert store.note_get(tmp_path, store.OWNERS_NS, "d-racetoc") == first, (
        "the second claim overwrote the first owner: the owner note was written with no "
        "compare-and-set, so the gate's stale 'unowned' read decided a write that landed "
        "after the room had an owner"
    )
    assert lost.status_code != 200, f"the losing claim was accepted ({lost.status_code})"


def test_a_broken_harness_fails_the_build_instead_of_reporting_the_bug(tmp_path):
    """The marker above is only load-bearing if it can still tell those two apart.

    This runs the two shapes under a real pytest, in a throwaway directory, and checks that
    the report separates them: the defect's own `AssertionError` is the expected xfail, and
    a `_plumbing` failure -- what a dead hook or a moved helper becomes -- is a failure that
    stops the build. The bodies are stubs on purpose; what is under test is the marker, not
    the race, and a stub cannot itself rot into passing for some unrelated reason.
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        textwrap.dedent("""
            import pytest

            def _plumbing(ok, what):
                if not ok:
                    pytest.fail(what)

            @pytest.mark.xfail(strict=True, raises=AssertionError, reason="probe")
            def test_the_harness_broke():
                _plumbing(False, "the race never happened")

            @pytest.mark.xfail(strict=True, raises=AssertionError, reason="probe")
            def test_the_defect_reproduced():
                assert False, "the second claim overwrote the first owner"
        """),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert "1 failed" in proc.stdout, f"a broken harness was not a failure:\n{proc.stdout}"
    assert "1 xfailed" in proc.stdout, f"the defect was not the expected failure:\n{proc.stdout}"
