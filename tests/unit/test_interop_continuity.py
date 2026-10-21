"""Resumed-page supplement using sirpold's unchanged #545 bridge test harness."""

import pytest
from test_interop_bridge import Bridge, EndOfPollsError, _export, _view
from test_interop_bridge import bridge_script as bridge_script


@pytest.mark.parametrize("source", ["read", "export"])
@pytest.mark.parametrize(
    ("saved_seq", "seqs"),
    [
        (1, (2, 4)),
        (0, (1, 3)),
        (1, (2, 2, 3)),
        (1, (2, 4, 3)),
    ],
    ids=["internal-hole", "saved-zero", "duplicate", "out-of-order"],
)
def test_resumed_batch_refuses_discontinuity_before_delivery(
    bridge_script, source, saved_seq, seqs
):
    checkpoint = {"generation": 1, "last_delivered_seq": saved_seq}
    view = _view(*seqs) if source == "read" else _view(max(seqs))
    exports = () if source == "read" else (_export(*seqs),)
    bridge = Bridge(checkpoint, [view], exports)

    with pytest.raises(RuntimeError, match=f"non-contiguous records after bridge seq {saved_seq}"):
        bridge.run(bridge_script)

    assert bridge.delivered == []
    assert bridge.saved == []
    assert bridge.checkpoint == checkpoint
    assert bridge.export_calls == len(exports)


def test_continuity_is_required_after_cold_start_saves_its_first_batch(bridge_script):
    bridge = Bridge(None, [_view(4, 5), _view(6, 8)], [_export(4, 5)])

    with pytest.raises(RuntimeError, match="non-contiguous records after bridge seq 5"):
        bridge.run(bridge_script)

    assert [message["seq"] for message in bridge.delivered] == [4, 5]
    assert bridge.saved == [{"generation": 1, "last_delivered_seq": 5}]
    assert bridge.checkpoint == bridge.saved[0]
    assert bridge.cursors == [0, 5]
    assert bridge.export_calls == 1


def test_discontinuity_is_refused_before_filtering_bridge_echoes(bridge_script):
    checkpoint = {"generation": 1, "last_delivered_seq": 1}
    view = _view(2, 4)
    view["messages"][1]["from"] = "bridge-did"
    bridge = Bridge(checkpoint, [view])

    with pytest.raises(RuntimeError, match="non-contiguous records after bridge seq 1"):
        bridge.run(bridge_script)

    assert bridge.delivered == []
    assert bridge.saved == []
    assert bridge.checkpoint == checkpoint


def test_contiguous_bridge_echo_counts_toward_the_checkpoint(bridge_script):
    checkpoint = {"generation": 1, "last_delivered_seq": 1}
    view = _view(2, 3, 4)
    view["messages"][1]["from"] = "bridge-did"
    bridge = Bridge(checkpoint, [view])

    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)

    assert [message["seq"] for message in bridge.delivered] == [2, 4]
    assert bridge.saved == [{"generation": 1, "last_delivered_seq": 4}]
    assert bridge.checkpoint == bridge.saved[0]
    assert bridge.export_calls == 0
