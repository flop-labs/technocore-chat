"""Run: uv run --group dev python -m pytest tests/unit/test_signed_nonce_shape.py

`store.append` is typed `did: str | None, nonce: int | None`, which permits every pairing of
the two — including a DID with no nonce at all. `_write_record` decides otherwise and refuses,
but nothing in the suite exercised the refusal: on `dc8accc`, `coverage report -m` lists
`src/store.py` line 2298 among the missed lines, and no test mentions its message.

What sits behind the check is why it exists. Replay protection compares `nonce <= previous`,
so a nonce that is not an integer reaches a comparison it cannot answer — with `None` that is
`None <= int`, a `TypeError`, which surfaces as a 500 on the replay-protection path where the
honest answer is a refusal naming the bad argument. The HTTP lane cannot get there: it matches
the query parameter against `didkey.NONCE_PATTERN` (`[0-9]{1,19}`) and passes it through
`int()`. So this is the contract the store keeps for its in-process callers, and the one a
second implementation reads off the signature rather than off the wire.

These tests pin the refusal, the diagnosis it carries, and its position — before anything is
written, and before the replay window is consulted. Every case is a behaviour that should
outlive a tightening of the check, so none of them has to be deleted to fix one.
"""

import pytest
from _client import _keypair

# The expected text is `repr`, because that is what the message interpolates: a caller is
# shown what it actually sent, which is the difference between a diagnosis and a 403.
BAD_NONCES = [
    pytest.param(None, "None", id="missing"),
    pytest.param(-1, "-1", id="negative"),
    pytest.param("7", "'7'", id="digit-string"),
    pytest.param(1756731000000.0, "1756731000000.0", id="millisecond-clock-as-float"),
]


@pytest.mark.parametrize(("nonce", "shown"), BAD_NONCES)
def test_a_signed_write_refuses_a_nonce_that_is_not_a_non_negative_int(tmp_path, nonce, shown):
    """Each case is a plausible caller mistake rather than an abstract type violation.

    `None` is the default the signature already allows; `-1` satisfies `isinstance` and fails
    the range; `"7"` is a query parameter that never went through `int()`; and the float is
    `time.time() * 1000`, which is the "millisecond clock" the refusal itself recommends. The
    advice is sound and the type is not, which is exactly why the message has to name the
    argument instead of only saying no.
    """
    import store

    did, _ = _keypair()
    with pytest.raises(store.StoreError, match="non-negative integer nonce") as refused:
        store.append(tmp_path, "lobby", "", "hello", did=did, nonce=nonce)
    assert f"got {shown}" in str(refused.value)


def test_a_refused_nonce_writes_nothing_at_all(tmp_path):
    """`test_rejected_write_leaves_no_lock_file` pins this for a capacity refusal, which is
    reached under the create gate. The shape check is earlier still — ahead of `_reap` and
    ahead of the gate — so the stronger claim holds: the store is untouched, not merely
    consistent. That is worth a test rather than a reading, because a refusal that created
    the room would let a caller with a malformed nonce spend room-cap slots it never used.
    """
    import store

    did, _ = _keypair()
    with pytest.raises(store.StoreError, match="non-negative integer nonce"):
        store.append(tmp_path, "fresh", "", "hello", did=did, nonce=None)
    assert not store.room_path(tmp_path, "fresh").exists()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_the_shape_refusal_comes_before_the_replay_window_is_consulted(tmp_path):
    """Both checks refuse a bad nonce; only one of them can say what was wrong.

    With a nonce already recorded for this key in this room, a `None` nonce could plausibly
    come back as a replay refusal — "not greater than 4" — and that would be a false
    diagnosis: nothing was replayed, and the caller's counter is not the problem. It comes
    back as the shape refusal instead.

    The order is also what makes the second nonce guard inside the create gate unreachable:
    by the time control is under the lock on the signed branch, `nonce` has already been
    proved a non-negative int, so a `None` cannot arrive there. Reorder the two and this test
    is what tells you.
    """
    import store

    did, _ = _keypair()
    store.append(tmp_path, "lobby", "", "first", did=did, nonce=4)

    with pytest.raises(store.StoreError, match="non-negative integer nonce") as refused:
        store.append(tmp_path, "lobby", "", "second", did=did, nonce=None)
    message = str(refused.value)
    assert "not greater than" not in message
    assert "must carry a nonce" not in message


def test_zero_is_a_valid_nonce_and_is_spent_by_being_used(tmp_path):
    """The refusal is written `nonce < 0`, not `nonce <= 0`, so a counter that starts at zero
    is admissible — and having used it, the same key must count up.

    Both halves, because the second is where a plausible narrowing hides: `_last_nonce`
    returning `0` has to read as "zero was used", not as "no nonce recorded". That is
    `previous is not None`, not `if previous`, and a test that only asserted the refusal side
    would pass either way.
    """
    import store

    did, _ = _keypair()
    assert store.append(tmp_path, "lobby", "", "hello", did=did, nonce=0)["nonce"] == 0
    with pytest.raises(store.StoreError, match="nonce 0 is not greater than 0"):
        store.append(tmp_path, "lobby", "", "again", did=did, nonce=0)


def test_the_unsigned_lane_never_looks_at_the_nonce(tmp_path):
    """§5.2 keeps the unsigned lane forever, and the nonce is not part of it: with no `did`
    there is no key to protect from replay and nothing to count up.

    So the argument is not validated here — it is not consulted. `-1` is the same value the
    signed lane refuses two tests up, which locates the rule where it belongs: not a check on
    the parameter, but a property of the signed lane. The record it writes carries no `nonce`
    field either, so a reader has nothing to mistake for provenance that was never proved.
    """
    import store

    rec = store.append(tmp_path, "lobby", "bot", "hello", nonce=-1)
    assert "nonce" not in rec
