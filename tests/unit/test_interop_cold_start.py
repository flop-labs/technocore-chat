"""Cold-start snapshot and rolling-upgrade regressions for the published loop."""

import pytest
from test_interop_bridge import Bridge, EndOfPollsError, _export, _view
from test_interop_bridge import bridge_script as bridge_script


@pytest.mark.parametrize("first_seq", [1, 101])
def test_cold_start_exports_beyond_default_fifty_record_window(bridge_script, first_seq):
    seqs = list(range(first_seq, first_seq + 75))
    bridge = Bridge(None, [_view(*seqs[-50:])], [_export(*seqs)])
    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)
    assert [m["seq"] for m in bridge.delivered] == seqs
    assert bridge.saved == [{"generation": 1, "last_delivered_seq": seqs[-1]}]
    assert bridge.export_calls == 1


@pytest.mark.parametrize("poll_seqs", [(), (1, 2)])
def test_cold_start_always_uses_export_even_without_poll_gap(bridge_script, poll_seqs):
    bridge = Bridge(None, [_view(*poll_seqs, last_seq=2)], [_export(1, 2, 3)])
    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)
    assert [m["seq"] for m in bridge.delivered] == [1, 2, 3]
    assert bridge.saved == [{"generation": 1, "last_delivered_seq": 3}]
    assert bridge.export_calls == 1


def test_cold_start_generation_race_retries_before_adopting(bridge_script):
    bridge = Bridge(
        None,
        [_view(1), _view(2, generation=2)],
        [_export(2, generation=2), _export(2, 3, generation=2)],
    )
    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)
    assert [m["seq"] for m in bridge.delivered] == [2, 3]
    assert bridge.saved == [{"generation": 2, "last_delivered_seq": 3}]
    assert bridge.cursors == [0, 0, 3]
    assert bridge.export_calls == 2


@pytest.mark.parametrize("cold_start", [False, True])
@pytest.mark.parametrize("missing_poll", [False, True])
@pytest.mark.parametrize("missing_header", [False, True])
def test_absent_generation_normalizes_to_zero(
    bridge_script, cold_start, missing_poll, missing_header
):
    view = _view(3, generation=0)
    export = _export(1, 2, 3, generation=0)
    if missing_poll:
        del view["generation"]
    if missing_header:
        export.headers.clear()
    checkpoint = None if cold_start else {"generation": 0, "last_delivered_seq": 1}
    bridge = Bridge(checkpoint, [view], [export])
    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)
    assert [m["seq"] for m in bridge.delivered] == ([1, 2, 3] if cold_start else [2, 3])
    assert bridge.saved == [{"generation": 0, "last_delivered_seq": 3}]


def test_missing_generation_still_refuses_nonzero_saved_generation(bridge_script):
    view = _view(4)
    del view["generation"]
    checkpoint = {"generation": 1, "last_delivered_seq": 3}
    bridge = Bridge(checkpoint, [view])
    with pytest.raises(RuntimeError, match="generation 1 -> 0"):
        bridge.run(bridge_script)
    assert bridge.delivered == []
    assert bridge.saved == []
    assert bridge.checkpoint == checkpoint
