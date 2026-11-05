"""The note gauge must retain its own freshness policy inside a cached room listing."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from queue import Queue
from threading import Barrier
from types import SimpleNamespace

import _client
import pytest
from _client import _age

client = _client.client


@pytest.mark.parametrize("rooms_ttl,notes_ttl", [(3, 30), (60, 0)])
@pytest.mark.parametrize("writer", ["http", "store"])
def test_note_writes_refresh_the_gauge_without_rewalking_rooms(
    client, monkeypatch, rooms_ttl, notes_ttl, writer
):
    import app
    import config
    import store

    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 120.0))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    assert client.get("/r/gauge-room/say/bot/first").status_code == 200
    room_views = []
    real = store.room_stats

    def counted(*args, **kwargs):
        view = real(*args, **kwargs)
        room_views.append(view)
        return view

    monkeypatch.setattr(store, "room_stats", counted)
    with config.override(ROOMS_CACHE_SECONDS=rooms_ttl, NOTE_STATS_CACHE_SECONDS=notes_ttl):
        before = client.get("/rooms?format=json").json()
        retained = deepcopy(room_views[0])
        seen = before["engagement"]["windowed_messages"]
        assert before["notes"]["total"] == 0 and seen > 0
        assert client.get("/r/gauge-room/say/bot/second").status_code == 200

        if writer == "http":
            assert client.get("/kv/plans/next/set/ship-it").status_code == 200
        else:
            store.note_set(config.ROOT, "plans", "next", "ship-it")

        after = client.get("/rooms?format=json").json()
        text = client.get("/rooms").text
        assert after["notes"]["total"] == 1
        assert after["notes"]["bytes"] == len(b"ship-it")
        assert after["engagement"]["windowed_note_to_message_ratio"] == round(1 / seen, 4)
        assert "# notes 1 of" in text and f"notes/msg {1 / seen:.2f}" in text
        assert after["rooms"] == before["rooms"], "room recency still uses its own cache"
        assert len(room_views) == 1, "ordinary notes must not invalidate the costly room walk"
        assert room_views[0] == retained, "rendering must not mutate a shared cached view"


def test_disabling_the_note_cache_takes_effect_inside_a_cached_listing(client, monkeypatch):
    import app
    import config
    import store

    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 120.0))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    calls = []
    real = store.note_stats

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(store, "note_stats", counted)
    with config.override(ROOMS_CACHE_SECONDS=60, NOTE_STATS_CACHE_SECONDS=30):
        client.get("/rooms")
        client.get("/rooms")
        assert len(calls) == 1
        with config.override(NOTE_STATS_CACHE_SECONDS=0):
            client.get("/rooms")
            client.get("/rooms")
        assert len(calls) == 3, "zero must bypass the note cache on each room-cache hit"


def test_reaped_notes_expire_on_the_note_timer_without_rewalking_rooms(client, monkeypatch):
    import app
    import config
    import store

    clock = [120.0]
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    with config.override(ROOMS_CACHE_SECONDS=60, NOTE_STATS_CACHE_SECONDS=10):
        store.note_set(config.ROOT, "plans", "old", "v")
        assert client.get("/rooms?format=json").json()["notes"]["total"] == 1
        cache = app._rooms_walk.cache_info()
        _age(store.note_path(config.ROOT, "plans", "old"), store.IDLE_SECONDS + 60)
        store._reap_pass(config.ROOT, store.time.time())
        assert store.note_stats(config.ROOT)["total"] == 0

        clock[0] = 129.0
        assert client.get("/rooms?format=json").json()["notes"]["total"] == 1
        clock[0] = 131.0
        assert client.get("/rooms?format=json").json()["notes"]["total"] == 0
        assert app._rooms_walk.cache_info().misses == cache.misses


def test_concurrent_room_renders_keep_their_note_gauges_isolated(client, monkeypatch):
    import app
    import config
    import store

    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 120.0))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    assert client.get("/r/gauge-room/say/bot/first").status_code == 200
    with config.override(ROOMS_CACHE_SECONDS=60):
        before = client.get("/rooms?format=json").json()
        cached = app._rooms_view(50)
        retained = deepcopy(cached)
        cache_info = app._rooms_walk.cache_info()
        seen = before["engagement"]["windowed_messages"]
        assert seen > 0

        snapshots = Queue()
        for total in (1, 3):
            snapshots.put({**before["notes"], "total": total, "bytes": total * 7})
        got_views = Barrier(2, timeout=10)
        ready_to_render = Barrier(2, timeout=10)
        real_respond = app.respond

        def note_snapshot():
            snapshot = snapshots.get_nowait()
            got_views.wait()  # Both requests have acquired the same cached room view.
            return snapshot

        def render_together(*args, **kwargs):
            # Both ratios must be assigned before either JSON response is serialized.
            # A missing nested copy then makes at least one response use the other's ratio.
            ready_to_render.wait()
            return real_respond(*args, **kwargs)

        def read_rooms():
            reader = type(client)(app.app)
            try:
                return reader.get("/rooms?format=json")
            finally:
                reader.close()

        monkeypatch.setattr(app, "_note_stats", note_snapshot)
        monkeypatch.setattr(app, "respond", render_together)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(read_rooms) for _ in range(2)]
            try:
                responses = [future.result(timeout=15) for future in pending]
            finally:
                got_views.abort()
                ready_to_render.abort()

        assert all(response.status_code == 200 for response in responses)
        views = [response.json() for response in responses]
        assert sorted(view["notes"]["total"] for view in views) == [1, 3]
        for view in views:
            assert view["engagement"]["windowed_messages"] == seen
            assert view["engagement"]["windowed_note_to_message_ratio"] == round(
                view["notes"]["total"] / seen, 4
            )
        assert cached == retained, "concurrent rendering must not mutate the cached view"
        assert app._rooms_walk.cache_info().misses == cache_info.misses
        assert app._rooms_walk.cache_info().hits == cache_info.hits + 2
