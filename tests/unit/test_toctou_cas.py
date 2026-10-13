"""Run: uv run --group dev python -m pytest tests"""

import config
import store
import _client
from _client import _keypair, _set_signed

client = _client.client  # the shared TestClient fixture


def test_cas_prevents_stale_owner_overwrite(client, monkeypatch):
    """A concurrent ownership transfer landing between _burn_nonce and note_set must be refused.

    Scenario: Owner A claims room. A signs a re-assertion. Mid-flight, we simulate
    that the owner changed to B. The CAS check must see this, set expect_absent=True,
    and since the note is no longer absent, note_set must return 409.
    """
    did_a, sign_a = _keypair(1)
    did_b, sign_b = _keypair(2)
    
    # CRITICAL: Ownable rooms MUST start with "d-" per store.ownable() rules.
    room = "d-cas-room"

    # 1. A claims the room (nonce 1)
    assert _set_signed(client, "room-owners", room, did_a, sign_a, did_a, nonce=1).status_code == 200

    # 2. Intercept note_get to simulate race: Gate sees A (passes), CAS check sees B (fails)
    original_note_get = store.note_get
    room_get_count = 0

    def mock_note_get(root, ns, key):
        nonlocal room_get_count
        if ns == store.OWNERS_NS and key == room:
            room_get_count += 1
            if room_get_count == 1:
                return did_a  # Gate check: signer == current, passes
            else:
                return did_b  # CAS check: current != signer, triggers CAS failure
        return original_note_get(root, ns, key)

    monkeypatch.setattr(store, "note_get", mock_note_get)

    # 3. A tries to re-assert ownership (nonce 3).
    # The CAS check will see owner is did_b, not did_a.
    # It will set expect_absent=True. Since the note exists, note_set returns 409.
    response = _set_signed(client, "room-owners", room, did_a, sign_a, did_a, nonce=3)
    
    assert response.status_code == 409, f"Expected 409 Conflict, got {response.status_code}: {response.text}"
    
    # 4. Verify the stale write was blocked: the owner on disk remains did_a.
    # (We never actually wrote did_b to disk in this simulation, we only mocked the read).
    assert original_note_get(config.ROOT, store.OWNERS_NS, room) == did_a
