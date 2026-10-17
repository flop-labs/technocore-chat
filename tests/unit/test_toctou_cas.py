"""Run: uv run --group dev python -m pytest tests"""

import config
import store
import _client
from _client import _keypair, _set_signed

client = _client.client  # the shared TestClient fixture


def test_owner_changed_after_reread_is_not_overwritten(client, monkeypatch):
    """Interleaved probe: a concurrent handover that lands between _burn_nonce and
    note_set must be refused with 409; the intermediate owner must be preserved.

    Scenario: A owns the room. A signs a handover to C. A concurrent handover to B
    lands inside note_set before A's write takes the lock. With expect=signer the
    CAS inside the store lock catches the mismatch and returns 409.
    """
    did_a, sign_a = _keypair(1)
    did_b, _ = _keypair(2)
    did_c, _ = _keypair(3)

    room = "d-race-room"

    # A claims the room (nonce 1)
    assert _set_signed(client, "room-owners", room, did_a, sign_a, did_a, nonce=1).status_code == 200

    real_set, fired = store.note_set, []

    def racing_set(root, ns, key, value, *a, **kw):
        if ns == store.OWNERS_NS and key == room and not fired:
            fired.append(1)
            real_set(root, ns, key, did_b)  # concurrent handover to B lands here
        return real_set(root, ns, key, value, *a, **kw)

    monkeypatch.setattr(store, "note_set", racing_set)

    # A tries to hand the room to C (nonce 3); the race fires first, giving it to B
    r = _set_signed(client, "room-owners", room, did_a, sign_a, did_c, nonce=3)
    assert fired, "racing_set was never called — race did not fire"
    assert r.status_code == 409, f"Expected 409 Conflict, got {r.status_code}: {r.text}"
    assert store.note_get(config.ROOT, store.OWNERS_NS, room) == did_b
