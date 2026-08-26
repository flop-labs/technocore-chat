"""The manual's rendered capacity numbers must survive operators raising
CHAT_MAX_ROOMS: the per-room retention floor is total/rooms, so at the
production cap of 10240 it is 512 KiB, and a whole-MiB formatter floors that
to "0 MiB" — publishing the opposite of the guarantee the store enforces (#242).
"""

from __future__ import annotations

import app
import store


def test_fmt_bytes_keeps_whole_mib_values_unchanged():
    # Default-config renderings must stay byte-identical: 10 MiB ring, 1 MiB floor.
    assert store.fmt_bytes(10 * 1024 * 1024) == "10 MiB"
    assert store.fmt_bytes(1024 * 1024) == "1 MiB"


def test_fmt_bytes_does_not_floor_sub_unit_values_to_zero():
    # 5 GiB / 10240 rooms = 512 KiB: the value that shipped as "0 MiB" (#242).
    assert store.fmt_bytes(524288) == "512 KiB"
    assert store.fmt_bytes(65536) == "64 KiB"
    assert store.fmt_bytes(512) == "512 B"
    assert store.fmt_bytes(0) == "0 B"


def test_manual_default_config_rendering_is_unchanged():
    assert "past ~10 MiB" in app._render_manual()
    assert "a guaranteed\n1 MiB per room" in app._render_manual()


def test_manual_renders_the_production_floor_not_zero(monkeypatch):
    # The live deployment runs CHAT_MAX_ROOMS=10240, halving the floor to 512 KiB.
    monkeypatch.setattr(store, "MAX_ROOMS", 10240)
    monkeypatch.setattr(
        store, "RESERVED_ROOM_BYTES", store.MAX_TOTAL_ROOM_BYTES // store.MAX_ROOMS
    )
    rendered = app._render_manual()
    assert "512 KiB per room" in rendered
    assert "0 MiB per room" not in rendered
