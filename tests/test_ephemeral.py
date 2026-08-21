"""Tests for ephemeral rooms (e-): TTL boundaries, pagination stability, compound prefixes, and corrupt timestamps."""

from __future__ import annotations

import datetime
import json
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app as app_module  # noqa: E402
import store  # noqa: E402

TTL = store.EPHEMERAL_TTL_SECONDS


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    monkeypatch.setattr(app_module, "ROOT", tmp_path)
    return TestClient(app_module.app)


def _iso(epoch_seconds: float) -> str:
    """Format unix timestamp into standard ISO8601 UTC string matching store._now()."""
    dt = datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -----------------------------------------------------------------------------
# 1. TTL Expiration Boundary Tests (now - (TTL - 1) vs now - (TTL + 1))
# -----------------------------------------------------------------------------


def test_ephemeral_exact_ttl_boundary_retention(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = "e-boundary"
    t0 = 1_700_000_000.0

    # Write msg1 at t0, msg2 at t0 + 2
    monkeypatch.setattr(time, "time", lambda: t0)
    monkeypatch.setattr(store, "_now", lambda: _iso(t0))
    client.get(f"/r/{room}/say/bot/msg_boundary_expired")

    monkeypatch.setattr(time, "time", lambda: t0 + 2)
    monkeypatch.setattr(store, "_now", lambda: _iso(t0 + 2))
    client.get(f"/r/{room}/say/bot/msg_boundary_retained")

    # Move current time to t0 + TTL + 1:
    # msg1 age is (TTL + 1) -> Expired (dropped)
    # msg2 age is (TTL - 1) -> Retained (within TTL)
    now = t0 + TTL + 1
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(store, "_now", lambda: _iso(now))

    view = client.get(f"/r/{room}?format=json").json()
    texts = [m["text"] for m in view["messages"]]

    assert "msg_boundary_expired" not in texts
    assert "msg_boundary_retained" in texts
    assert view["count"] == 1
    assert view["first_seq"] == 2
    assert view["last_seq"] == 2


# -----------------------------------------------------------------------------
# 2. Pagination Stability Across Expired Windows
# -----------------------------------------------------------------------------


def test_ephemeral_pagination_cursor_continuity_across_expired_gaps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    room = "e-cursor-gap"
    t0 = 1_700_000_000.0

    # Write 3 messages early (bind i in closures to avoid B023)
    for i in range(1, 4):
        monkeypatch.setattr(time, "time", lambda i=i: t0 + i)
        monkeypatch.setattr(store, "_now", lambda i=i: _iso(t0 + i))
        client.get(f"/r/{room}/say/bot/batch1_msg{i}")

    # Reader records last_seq cursor = 3
    view1 = client.get(f"/r/{room}?format=json").json()
    assert view1["last_seq"] == 3
    assert view1["count"] == 3

    # Fast-forward past TTL + 50s so batch1 is fully expired
    t1 = t0 + TTL + 50
    monkeypatch.setattr(time, "time", lambda: t1)
    monkeypatch.setattr(store, "_now", lambda: _iso(t1))

    # Room is now empty on read; disk last_seq remains monotonic
    view_empty = client.get(f"/r/{room}?format=json").json()
    assert view_empty["count"] == 0
    assert len(view_empty["messages"]) == 0
    assert store.last_seq(tmp_path, room) == 3

    # Append 2 new messages in the new active window
    client.get(f"/r/{room}/say/bot/batch2_msg4")
    client.get(f"/r/{room}/say/bot/batch2_msg5")

    # Read with existing cursor since=3: receives only new messages without rewind or crash
    view_since = client.get(f"/r/{room}?since=3&format=json").json()
    assert [m["seq"] for m in view_since["messages"]] == [4, 5]
    assert [m["text"] for m in view_since["messages"]] == ["batch2_msg4", "batch2_msg5"]
    assert view_since["first_seq"] == 4
    assert view_since["last_seq"] == 5

    # Reading with since=0 shows smooth exposure of the gap (first_seq is 4, not 1)
    view_all = client.get(f"/r/{room}?format=json").json()
    assert view_all["count"] == 2
    assert view_all["first_seq"] == 4


# -----------------------------------------------------------------------------
# 3. Compound Prefix Variations (e-p-, mb-e-, etc.)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("room_name", "is_mb"),
    [
        ("e-p-private-temp", False),
        ("mb-e-secure-temp", True),
        ("p-e-inverted-classes", False),
    ],
)
def test_ephemeral_compound_prefixes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, room_name: str, is_mb: bool
) -> None:
    assert store.is_ephemeral(room_name) is True
    t0 = 1_700_000_000.0

    monkeypatch.setattr(time, "time", lambda: t0)
    monkeypatch.setattr(store, "_now", lambda: _iso(t0))

    if is_mb:
        res_unsigned = client.get(f"/r/{room_name}/say/bot/hello")
        assert res_unsigned.status_code == 403
    else:
        res = client.get(f"/r/{room_name}/say/bot/live_now")
        assert res.status_code == 200

        # Advance past TTL
        monkeypatch.setattr(time, "time", lambda: t0 + TTL + 10)
        view = client.get(f"/r/{room_name}?format=json").json()
        assert view["count"] == 0
        assert len(view["messages"]) == 0


# -----------------------------------------------------------------------------
# 4. Corrupt / Malformed Timestamps Fail-Closed
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_ts",
    [
        "not-a-date",
        "2020-99-99T99:99:99Z",
        "",
        None,
        123456789,  # Non-string type
    ],
)
def test_ephemeral_corrupt_timestamps_treated_as_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_ts: object
) -> None:
    room = "e-corrupt-ts"
    now_epoch = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: now_epoch)

    room_file = store.room_path(tmp_path, room)
    room_file.parent.mkdir(parents=True, exist_ok=True)

    corrupt_record = {
        "seq": 1,
        "ts": bad_ts,
        "from": "bot",
        "text": "corrupt_payload",
    }
    valid_record = {
        "seq": 2,
        "ts": _iso(now_epoch),
        "from": "bot",
        "text": "valid_payload",
    }

    with open(room_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(corrupt_record) + "\n")
        f.write(json.dumps(valid_record) + "\n")

    view = store.read_messages(tmp_path, room)
    assert view["count"] == 1
    assert len(view["messages"]) == 1
    assert view["messages"][0]["text"] == "valid_payload"
    assert view["first_seq"] == 2
    assert view["last_seq"] == 2


# -----------------------------------------------------------------------------
# 5. POST & GET Parity for Writes and Compaction
# -----------------------------------------------------------------------------


def test_ephemeral_post_get_parity_and_compaction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    room = "e-parity-compaction"
    t0 = 1_700_000_000.0

    # 1. Post via GET and POST in initial window
    monkeypatch.setattr(time, "time", lambda: t0)
    monkeypatch.setattr(store, "_now", lambda: _iso(t0))
    res_get = client.get(f"/r/{room}/say/agent_get/first_get", params={"format": "json"})
    assert res_get.status_code == 200

    res_post = client.post(
        f"/r/{room}",
        params={"format": "json"},
        json={"from": "agent_post", "text": "first_post"},
    )
    assert res_post.status_code == 200

    # 2. Advance time past TTL + 1
    t1 = t0 + TTL + 1
    monkeypatch.setattr(time, "time", lambda: t1)
    monkeypatch.setattr(store, "_now", lambda: _iso(t1))

    view = client.get(f"/r/{room}?format=json").json()
    assert view["count"] == 0
    assert len(view["messages"]) == 0

    # 3. New POST write in new window appends monotonically
    res_post_new = client.post(
        f"/r/{room}",
        params={"format": "json"},
        json={"from": "agent_post", "text": "second_post"},
    )
    assert res_post_new.status_code == 200
    assert res_post_new.json()["posted"]["seq"] == 3
    assert res_post_new.json()["first_seq"] == 3
    assert res_post_new.json()["count"] == 1
