"""Evasion messages must model distinct identities, not reusable peer identities."""

import importlib.util
import random
from pathlib import Path


def test_evasion_senders_match_the_measured_distinct_identity_shape():
    spec = importlib.util.spec_from_file_location(
        "dupe_filter_bench", Path(__file__).resolve().parents[2] / "bench" / "dupe_filter.py"
    )
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    rows = bench.build_corpus(random.Random(20260827))
    senders = bench.senders_for(rows, random.Random(20260828))
    evasion_senders = [
        sender
        for sender, (_, label) in zip(senders, rows, strict=True)
        if label == "evasion"
    ]

    farm_senders = [
        sender
        for sender, (_, label) in zip(senders, rows, strict=True)
        if label.startswith("farm")
    ]
    peer_senders = [
        sender
        for sender, (_, label) in zip(senders, rows, strict=True)
        if not label.startswith("farm") and label != "evasion"
    ]

    assert len(evasion_senders) == 720
    assert len(set(evasion_senders)) == len(evasion_senders)
    assert all(sender.startswith("did:key:evade") for sender in evasion_senders)
    assert all(sender.startswith("did:key:farm") for sender in farm_senders)
    assert all(sender.startswith("did:key:peer") for sender in peer_senders)
    # The farm/evasion identities are one-per-write, while legitimate traffic
    # reuses peers. Count every distinct identity exactly, not just a ratio floor.
    assert len(set(senders)) == len(farm_senders) + len(evasion_senders) + len(set(peer_senders))
    assert len(set(senders)) / len(senders) >= 0.80
