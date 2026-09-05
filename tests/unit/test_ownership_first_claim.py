"""Regression test for #173: two concurrent first-time ownership claims."""

import threading


def test_second_first_claim_gets_409(client):
    """When two first-time claims race, only one must win — the other gets 409.

    Without the fix, both note_set calls are unconditional, so the second
    silently overwrites the first. With expect_absent=True forced on a
    first claim, note_set's own lock rejects the loser.
    """
    from tests._client import _keypair, _say_signed

    did_a, sign_a = _keypair(seed=90)
    did_b, sign_b = _keypair(seed=91)
    room = "d-race-claim"

    # Claim A — should succeed
    resp_a = client.get(
        f"/kv/room-owners/{room}/set-signed/{did_a}/{sign_a(f'room-owners|{room}|1|{did_a}')}/1/{did_a}",
    )
    assert resp_a.status_code == 200, f"first claim failed: {resp_a.text}"

    # Claim B — same room, different key, should get 409 (already owned)
    resp_b = client.get(
        f"/kv/room-owners/{room}/set-signed/{did_b}/{sign_b(f'room-owners|{room}|1|{did_b}')}/1/{did_b}",
    )
    assert resp_b.status_code == 403, f"second claim should be refused: {resp_b.text}"
