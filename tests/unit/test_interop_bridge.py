"""Execute #545's published bridge; no service, network or identity is required."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge_script():
    document = (Path(__file__).resolve().parents[2] / "src" / "interop.md").read_text(
        encoding="utf-8"
    )
    return compile(document.split("```python", 1)[1].split("```", 1)[0], "interop.md", "exec")


def _messages(*seqs):
    return [{"seq": seq, "from": "other", "text": f"message {seq}"} for seq in seqs]


def _view(*seqs, generation=1, last_seq=None):
    return {
        "generation": generation,
        "first_seq": seqs[0] if seqs else None,
        "last_seq": seqs[-1] if last_seq is None else last_seq,
        "messages": _messages(*seqs),
    }


def _export(*seqs, generation=1):
    return SimpleNamespace(
        headers={"X-Room-Generation": str(generation)},
        text="".join(json.dumps(message) + "\n" for message in _messages(*seqs)),
    )


class EndOfPollsError(Exception):
    """Terminate the infinite example at its next read, never at checkpoint saving."""


class InterruptedError(Exception):
    """Simulate a delivery or atomic checkpoint operation failing before completion."""


class Bridge:
    def __init__(self, checkpoint, views, exports=()):
        self.checkpoint = deepcopy(checkpoint)
        self.views = iter(views)
        self.exports = iter(exports)
        self.delivered = []
        self.saved = []
        self.cursors = []
        self.export_calls = 0
        self.fail_delivery = None
        self.fail_save = False

    def get(self, url, **kwargs):
        if url.endswith("/export"):
            self.export_calls += 1
            try:
                return next(self.exports)
            except StopIteration:
                raise AssertionError("unexpected export request") from None
        self.cursors.append(kwargs["params"]["since"])
        view = next(self.views, None)
        if view is None:
            raise EndOfPollsError
        return SimpleNamespace(json=lambda: view)

    def deliver(self, message):
        if message["seq"] == self.fail_delivery:
            raise InterruptedError
        self.delivered.append(deepcopy(message))

    def save(self, room, checkpoint):
        assert room == "bridge"
        if self.fail_save:
            raise InterruptedError
        self.checkpoint = deepcopy(checkpoint)
        self.saved.append(deepcopy(checkpoint))

    def run(self, script):
        exec(
            script,
            {
                "BASE": "https://example.invalid",
                "BRIDGE_DID": "bridge-did",
                "room": "bridge",
                "get": self.get,
                "load_checkpoint": lambda _room: deepcopy(self.checkpoint),
                "save_checkpoint": self.save,
                "deliver_to_far_side": self.deliver,
            },
        )


CHECKPOINT = {"generation": 1, "last_delivered_seq": 3}


@pytest.mark.parametrize(
    ("checkpoint", "view", "exports", "delivered", "saved_seq"),
    [
        (CHECKPOINT, _view(4, 5), (), [4, 5], 5),
        (CHECKPOINT, _view(6, 7), (_export(1, 2, 3, 4, 5, 6, 7),), [4, 5, 6, 7], 7),
        # First observation has no claim on history before the retained floor.
        (None, _view(6, 7), (_export(4, 5, 6, 7),), [4, 5, 6, 7], 7),
        # Export may legitimately include records appended after the triggering poll.
        (CHECKPOINT, _view(6, 7), (_export(4, 5, 6, 7, 8),), [4, 5, 6, 7, 8], 8),
        (CHECKPOINT, _view(last_seq=3), (), [], 3),
        (None, _view(last_seq=0), (), [], 0),
    ],
    ids=["ordinary", "retained-prefix", "cold-start", "export-ahead", "caught-up", "empty-start"],
)
def test_bridge_delivery_precedes_checkpoint(
    bridge_script, checkpoint, view, exports, delivered, saved_seq
):
    bridge = Bridge(checkpoint, [view], exports)
    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)
    assert [message["seq"] for message in bridge.delivered] == delivered
    assert bridge.saved == [{"generation": 1, "last_delivered_seq": saved_seq}]
    assert bridge.export_calls == len(exports)


@pytest.mark.parametrize("export", [_export(5, 6), _export(1, 2, 3), _export()])
def test_export_without_checkpoint_successor_stops_before_delivery(bridge_script, export):
    bridge = Bridge(CHECKPOINT, [_view(6, 7)], [export])
    with pytest.raises(RuntimeError, match="retention gap after bridge seq 3"):
        bridge.run(bridge_script)
    assert bridge.delivered == []
    assert bridge.saved == []
    assert bridge.checkpoint == CHECKPOINT


def test_export_generation_mismatch_retries_without_advancing(bridge_script):
    bridge = Bridge(
        CHECKPOINT,
        [_view(6, 7), _view(6, 7)],
        [_export(4, 5, generation=2), _export(4, 5, 6, 7)],
    )
    with pytest.raises(EndOfPollsError):
        bridge.run(bridge_script)
    assert bridge.cursors == [3, 3, 7]
    assert bridge.export_calls == 2
    assert [message["seq"] for message in bridge.delivered] == [4, 5, 6, 7]
    assert bridge.saved == [{"generation": 1, "last_delivered_seq": 7}]


@pytest.mark.parametrize("failure", ["delivery", "checkpoint"])
def test_restart_replays_uncheckpointed_batch(bridge_script, failure):
    first = Bridge(CHECKPOINT, [_view(4, 5)])
    first.fail_delivery = 5 if failure == "delivery" else None
    first.fail_save = failure == "checkpoint"
    with pytest.raises(InterruptedError):
        first.run(bridge_script)
    assert first.checkpoint == CHECKPOINT
    assert first.saved == []
    assert [message["seq"] for message in first.delivered] == (
        [4] if failure == "delivery" else [4, 5]
    )

    restarted = Bridge(first.checkpoint, [_view(4, 5)])
    with pytest.raises(EndOfPollsError):
        restarted.run(bridge_script)
    assert restarted.cursors == [3, 5]
    assert [message["seq"] for message in restarted.delivered] == [4, 5]
    assert restarted.checkpoint == {"generation": 1, "last_delivered_seq": 5}


def test_restart_after_saved_batch_does_not_redeliver(bridge_script):
    first = Bridge(CHECKPOINT, [_view(4, 5)])
    with pytest.raises(EndOfPollsError):
        first.run(bridge_script)
    restarted = Bridge(first.checkpoint, [_view(last_seq=5)])
    with pytest.raises(EndOfPollsError):
        restarted.run(bridge_script)
    assert restarted.cursors == [5, 5]
    assert restarted.delivered == []
    assert restarted.checkpoint == first.checkpoint
