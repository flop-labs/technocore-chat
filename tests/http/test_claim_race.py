"""Run: uv run --group dev python -m pytest tests"""

import _client
from _client import _claim, _keypair, _race_before_lock

client = _client.client  # the shared TestClient fixture


def test_two_first_claims_with_distinct_nonces_cannot_both_own_a_room(
    client, tmp_path, monkeypatch
):
    """Two callers claiming one unowned room, each with its own nonce, must not both win.

    Closes #173 (was #628): the gate reads unowned without a lock, both claimers burn
    distinct nonces, so the nonce CAS cannot catch them. The gate's own snapshot is
    carried into the write as create-if-absent inside the store lock; the loser gets a
    non-200 and the first owner is preserved. Re-reading after the nonce burn would
    reintroduce the split-read (B lands between the two reads, A sees present and
    overwrites unconditionally).
    """
    import store

    first, _ = _keypair(seed=1)
    second, second_sign = _keypair(seed=2)
    owner_path = store.note_path(tmp_path, store.OWNERS_NS, "d-racetoc")

    def the_other_claim_lands():
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text(first, encoding="utf-8")

    raced = _race_before_lock(monkeypatch, store, owner_path, the_other_claim_lands)
    lost = _claim(client, "d-racetoc", second, second_sign, nonce=2)

    assert raced, "the race never happened -- this test proved nothing"
    assert store.note_get(tmp_path, store.OWNERS_NS, "d-racetoc") == first, (
        "the second claim overwrote the first owner: the owner note was written with no "
        "compare-and-set, so the gate's stale 'unowned' read decided a write that landed "
        "after the room had an owner"
    )
    assert lost.status_code != 200, f"the losing claim was accepted ({lost.status_code})"


def test_post_first_claim_race_cannot_overwrite_either(client, tmp_path, monkeypatch):
    """Same guarantee on the POST lane: the first owner wins, the loser is refused."""
    import store

    def _payload(ns, key, did, sign, value, nonce=1, **cond):
        swept = store.clean_text(value, store.MAX_VALUE_CHARS)
        return {
            "value": value,
            "did": did,
            "sig": sign(f"{ns}|{key}|{nonce}|{swept}"),
            "nonce": str(nonce),
            **cond,
        }

    first, _ = _keypair(seed=21)
    second, second_sign = _keypair(seed=22)
    owner_path = store.note_path(tmp_path, store.OWNERS_NS, "d-racepost")

    def the_other_claim_lands():
        owner_path.parent.mkdir(parents=True, exist_ok=True)
        owner_path.write_text(first, encoding="utf-8")

    raced = _race_before_lock(monkeypatch, store, owner_path, the_other_claim_lands)
    lost = client.post(
        "/kv/room-owners/d-racepost",
        json=_payload("room-owners", "d-racepost", second, second_sign, second, nonce=2),
    )

    assert raced, "the race never happened -- this test proved nothing"
    assert store.note_get(tmp_path, store.OWNERS_NS, "d-racepost") == first
    assert lost.status_code != 200, f"the losing POST claim was accepted ({lost.status_code})"
