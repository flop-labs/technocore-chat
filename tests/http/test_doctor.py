"""scripts/doctor.py reads what the server serves — so its readings are a contract.

The script exists because three server behaviours read as success while meaning
nothing of the kind: an absent room answers the same 200/count=0 as an existing
empty one, the reaper deletes a single-message room after a day, and /rooms shows
listed-room headroom while unlisted rooms hold cap space. Each classification is
pinned here twice: once on a hand-built feed (the promise), and once on the live
response the app actually serves (the drift guard) — protocol drift breaks these
tests before it breaks the script's users.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import _client  # noqa: F401 (imported for the fixture alias below)
from _client import _keypair, _say_signed

client = _client.client  # the shared TestClient fixture

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("doctor", ROOT / "scripts" / "doctor.py")
assert _spec is not None and _spec.loader is not None
doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doctor)


def _feed(client, room):
    r = client.get(f"/r/{room}?format=json&limit=50")
    assert r.status_code == 200
    return r.json()


# --- the promises, on hand-built feeds ---------------------------------------


def test_an_empty_feed_is_never_a_pass():
    """The core trap: 200/count=0 is identical for an existing empty room and an
    absent one, so no did and no feed content may turn it into a pass."""
    level, detail = doctor.classify_mailbox(
        {"room": "mb-p-ghost", "count": 0, "first_seq": None, "last_seq": 0, "messages": []},
        "did:key:z6MkExample",
    )
    assert level == "warn"
    assert "NOT proof" in detail


def test_a_foreign_room_is_not_proof_of_ownership():
    """count>0 alone shows *someone* wrote — the pass needs a message from the
    caller's own did, or the caller is reading someone else's mailbox as theirs."""
    feed = {"count": 1, "last_seq": 1, "messages": [{"seq": 1, "from": "did:key:z6MkSomeoneElse"}]}
    level, _ = doctor.classify_mailbox(feed, "did:key:z6MkExample")
    assert level == "warn"


def test_note_parsing_round_trips_the_pattern_3_shape():
    did = "did:key:z6MkExample"
    note = doctor.parse_note(f"{did} x25519:AAAA mailbox:mb-p-x")
    assert (note["did"], note["x25519"], note["mailbox"]) == (did, "AAAA", "mb-p-x")
    assert doctor.parse_note("not a note")["did"] is None


def test_fingerprint_is_the_pattern_3_convention():
    """First 16 hex of SHA-256 of the *full* did:key string, lowercase — pinned to a
    vector so a silent hashing change cannot ship."""
    assert doctor.fingerprint("did:key:z6MkExample") == "beac80774be09b62"


# --- the drift guards, on what the app actually serves -----------------------


def test_an_absent_room_really_does_answer_like_an_empty_one(client):
    """If the server ever starts 404ing absent rooms, the trap this script warns
    about is gone and the warning text becomes wrong — fail here, loudly."""
    feed = _feed(client, "mb-p-never-created")
    assert feed["count"] == 0
    level, _ = doctor.classify_mailbox(feed, "did:key:z6MkExample")
    assert level == "warn"


def test_a_signed_mailbox_classifies_as_proven_and_reaper_exposed(client):
    did, sign = _keypair()
    assert _say_signed(client, "mb-p-doctor", did, sign, "mailbox open").status_code == 200
    feed = _feed(client, "mb-p-doctor")
    level, detail = doctor.classify_mailbox(feed, did)
    assert level == "pass" and "1 visible message(s)" in detail
    risk = doctor.reaper_risk(feed)  # one message: the 24-hour deletion applies
    assert risk is not None and risk[0] == "warn" and "24 hours" in risk[1]
    assert _say_signed(client, "mb-p-doctor", did, sign, "second", nonce=2).status_code == 200
    assert doctor.reaper_risk(_feed(client, "mb-p-doctor")) is None


def test_the_rooms_aggregate_line_still_parses(client):
    """A p- room would not do here: unlisted rooms never reach /rooms, and with zero
    listed rooms the server serves its no-rooms banner instead of the aggregate line.
    A fresh listed room also keeps the stored size in the B tier, so the size unit the
    script's regex accepts stays pinned to every unit _size() can emit."""
    assert client.get("/r/doctor-cap/say/doc/x").status_code == 200
    hdr = doctor.parse_rooms_header(client.get("/rooms").text)
    assert hdr is not None
    assert hdr["listed"] >= 1 and hdr["cap"] >= hdr["listed"]


# --- the CLI surface ---------------------------------------------------------


def test_no_arguments_is_a_usage_error_not_a_crash():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "doctor.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert out.returncode == 2
    assert "--did" in out.stderr
