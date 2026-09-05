"""Run: uv run --group dev python -m pytest tests

The #628 regression, kept in its own file deliberately: it is the acceptance test for a
race, not for an implementation. #179, #501 and #629 each propose a different mechanism
for closing it, so the test that decides whether any of them did belongs somewhere all
three can carry unchanged.
"""

import _client
import pytest
from _client import _claim, _keypair, _race_before_lock

client = _client.client  # the shared TestClient fixture


@pytest.mark.xfail(
    strict=True,
    reason="#628 is open: the owner note is written with no compare-and-set. strict, so "
    "this fails the build the moment it starts passing -- whoever closes #628 deletes "
    "this marker in the same change, and the suite refuses to let it be forgotten.",
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
    #628 is open, and turns the fix into a build failure until the marker goes. Measured on
    merge with main at 82d9429: this xpasses on #629 (so #629 closes #628), still xfails on
    #501, and #179 cannot be measured -- it conflicts with main in four files.
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

    assert raced, "the race never happened -- this test proved nothing"
    # The room keeps the owner that got there first. Which status the loser sees is the
    # implementation's call -- 409 and 403 are both defensible -- but it must not be 200,
    # and the stored owner is not negotiable.
    assert store.note_get(tmp_path, store.OWNERS_NS, "d-racetoc") == first, (
        "the second claim overwrote the first owner: the owner note was written with no "
        "compare-and-set, so the gate's stale 'unowned' read decided a write that landed "
        "after the room had an owner"
    )
    assert lost.status_code != 200, f"the losing claim was accepted ({lost.status_code})"
