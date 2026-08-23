"""Regression checks for owned-room instructions in the served manual."""

import sys
from pathlib import Path

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_served_manual_requires_signed_initial_room_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    for mod in ("app", "store"):
        sys.modules.pop(mod, None)

    import app as app_module

    manual = TestClient(app_module.app).get("/llms.txt").text

    assert (
        "GET /kv/room-owners/d-<room>/set-signed/<did>/<sig>/<claim_nonce>/"
        "<the same did:key>?if_absent=1" in manual
    )
    assert "signature covers `room-owners|d-<room>|<claim_nonce>|<the same did:key>`" in manual
    assert "allow-list nonce must be greater than claim_nonce" in manual
    assert "GET /kv/room-owners/d-<room>/set/<your did:key>?if_absent=1" not in manual
