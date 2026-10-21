"""Empty-page supplement using sirpold's unchanged #545 bridge test harness."""

import pytest
from test_interop_bridge import Bridge, EndOfPollsError, _view
from test_interop_bridge import bridge_script as bridge_script


@pytest.mark.parametrize("saved_seq", [0, 3])
def test_saved_checkpoint_refuses_empty_advancement_before_delivery(bridge_script, saved_seq):
    checkpoint = {"generation": 1, "last_delivered_seq": saved_seq}
    bridge = Bridge(checkpoint, [_view(last_seq=6), _view(7)])

    with pytest.raises(RuntimeError, match=f"continuity unavailable after bridge seq {saved_seq}"):
        bridge.run(bridge_script)

    assert bridge.checkpoint == checkpoint
    assert bridge.saved == []
    assert bridge.delivered == []
    assert bridge.cursors == [saved_seq]
    assert bridge.export_calls == 0


@pytest.mark.parametrize("first_head", [0, 6])
def test_empty_cold_start_checkpoint_requires_continuity_on_next_poll(bridge_script, first_head):
    bridge = Bridge(None, [_view(last_seq=first_head), _view(last_seq=first_head + 2)])

    with pytest.raises(RuntimeError, match=f"continuity unavailable after bridge seq {first_head}"):
        bridge.run(bridge_script)

    assert bridge.saved == [{"generation": 1, "last_delivered_seq": first_head}]
    assert bridge.checkpoint == bridge.saved[0]
    assert bridge.delivered == []
    assert bridge.cursors == [0, first_head]
    assert bridge.export_calls == 0


def test_cold_start_adopts_empty_head_then_delivers_its_successor(bridge_script):
    bridge = Bridge(None, [_view(last_seq=6), _view(7)])

    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)

    assert [message["seq"] for message in bridge.delivered] == [7]
    assert bridge.saved == [
        {"generation": 1, "last_delivered_seq": 6},
        {"generation": 1, "last_delivered_seq": 7},
    ]
    assert bridge.checkpoint == bridge.saved[-1]
    assert bridge.export_calls == 0


@pytest.mark.parametrize(("saved_seq", "head"), [(0, 0), (3, 3), (9, 6)])
def test_caught_up_empty_and_past_head_clamp_remain_valid(bridge_script, saved_seq, head):
    bridge = Bridge({"generation": 1, "last_delivered_seq": saved_seq}, [_view(last_seq=head)])

    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)

    assert bridge.saved == [{"generation": 1, "last_delivered_seq": head}]
    assert bridge.checkpoint == bridge.saved[0]
    assert bridge.delivered == []
    assert bridge.export_calls == 0


def test_generation_mismatch_takes_precedence_over_empty_advancement(bridge_script):
    checkpoint = {"generation": 1, "last_delivered_seq": 3}
    bridge = Bridge(checkpoint, [_view(generation=2, last_seq=6)])

    with pytest.raises(RuntimeError, match="room bridge was recreated: generation 1 -> 2"):
        bridge.run(bridge_script)

    assert bridge.checkpoint == checkpoint
    assert bridge.saved == []
    assert bridge.delivered == []
    assert bridge.export_calls == 0
