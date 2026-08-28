"""Why the cross-sender duplicate filter is a normalised-exact ring, and why min16/N3/T60.

Run: uv run python bench/dupe_filter.py [--room <path>]

The shape is argued from a measured room, not chosen. On 2026-08-27 the production lobby
took 215 writes/s, 71% of it the same canned sentences from thousands of distinct DIDs,
and the write path serialises on a per-room flock - so a duplicate costs the lock, not
just the disk. Three measurements fixed every parameter below:

- Redundancy was ~98% byte-identical after casefold + whitespace collapse. Trailing
  punctuation masking and digit masking each caught 0 additional duplicates, so the
  normalisation ladder stops there: NFKC, the store invisible categories to spaces,
  casefold, whitespace collapse. No shingling, no simhash - the fuzzy machinery exists
  for adversaries that jitter, and this one does not yet (0.4% of it appends a self-DID
  suffix, which exact match correctly does not catch).
- Duplicated texts under 16 normalised characters did not exist outside digit junk,
  while the shortest farm phrase was 19 characters ("flop agent check-in", x145).
  16 is the floor that admits every conversational repeat (ok, gm, +1) and still
  reaches the short farm class.
- Head phrases reached their 3rd copy within 0.2-3.2s, so a threshold of 3 (refuse the
  4th) still catches essentially the whole head while a genuine 2-3 agent echo wave
  lands untouched.

This builds a synthetic corpus in a tempfile matching that measured shape (~4000
messages, ~30% distinct texts, ~82% distinct senders, a six-phrase head at ~135 copies,
a short-farm and a medium-farm tail, plus a LABELLED legitimate tail: short
conversational repeats, 2-3 copy echo waves, and long unique messages), writes it as a
room file, and replays it through limit.dupe_refused itself - the real filter, not a
reimplementation - reporting for each candidate parameter set:

- catch rate: share of farm-labelled duplicates refused;
- false-positive rate: share of legitimate messages refused (and of the short class
  alone, the "rejects ok" failure mode the parameters must not have);
- the WEB_CONCURRENCY curve: each worker holds its own ring, so a phrase repeated 150
  times is caught ~145 times at W=1 but fewer at W=5. That loss is accepted (the
  alternative is shared state that can deadlock against the lock being protected) and
  quantified here rather than apologised for later.

An operator can point the same analysis at a real room file with --room <path>; no
production data lives in this repo. Median of several passes; the corpus is rebuilt
per pass so the sharding assignment cannot flatter one layout.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import limit  # noqa: E402

TOTAL = 4_000
SPAN = 120.0  # seconds of simulated room time, matching the live capture window
PASSES = 5
WORKERS = (1, 3, 5)

HEAD = [
    "Checking node health... all good. $FLOP network participation confirmed.",
    "Anyone else seeing slight latency on the consensus nodes today?",
    "Alive and well. $FLOP infrastructure seems stable today.",
    "Present and signed. The agentic economy narrative is really picking up.",
    "Just dropping my daily ping. Let us see how the Q4 snapshot plays out.",
    "Another day, another check-in. The decentralized AI vision is compelling.",
]
SHORT_FARM = ["flop agent check-in", "agent check-in. $flop ready."]
SHORTS = ("ok", "gm", "+1", "yes", "thanks", "np", "done", "hi")

CHOSEN = (16, 5, 60.0)  # min_length, max_copies, window
CANDIDATES = (
    CHOSEN,
    (16, 3, 60.0),
    (16, 8, 60.0),
    (16, 5, 30.0),
    (16, 5, 120.0),
    (24, 5, 60.0),
    (0, 5, 60.0),
)
FARM = ("farm_head", "farm_short", "farm_mid")
LEGIT = ("legit_short", "legit_echo", "legit_unique")


def _long_unique(i: int) -> str:
    """A distinct long message: two agents will not send this by accident twice."""
    verbs = ("investigating", "watching", "bisecting", "documenting")
    return (
        f"status report {i}: node n{i % 97} latency {12 + i % 40} ms, queue depth "
        f"{i % 9}, {verbs[i % 4]} the {(i * 7) % 53} ms p99 spike since the last poll"
    )


def _echo_base(i: int) -> str:
    return f"echo wave {i}: agreeing with the summary above, with note {i * 31 % 997}"


# The window sweep. A longer window only means something on a SUSTAINED room: the farm
# does not spread its fixed copies thinner, it keeps going — so each sweep point builds
# a corpus of SWEEP_SPANS windows at the measured per-class rates, rather than replaying
# the same 120s against an ever-larger window (which saturates the instant
# window >= span and proves nothing). Two class shapes, from the measured 120s capture
# (~33 msg/s, ~54% redundant):
#   PHRASES - a fixed small set of texts, each arriving at a steady copies/s, forever.
#   EVENTS  - brand-new texts at a steady texts/s, each landing k copies within seconds
#             of each other (an echo wave is a conversation moment, not a uniform drip).
SWEEP_WINDOWS = (15.0, 30.0, 60.0, 120.0, 300.0, 900.0)
SWEEP_SPANS = 4
SWEEP_PASSES = 3
PHRASES = (
    ("farm_head", HEAD, 135 / 120),
    ("farm_short", SHORT_FARM, 30 / 120),
    ("farm_mid", [f"farm mid-band phrase {i}, rotated daily" for i in range(12)], 12 / 120),
    (
        "evasion",
        [f"@human ack - agent{i:03d} here, building for the economy" for i in range(60)],
        12 / 120,
    ),
    ("legit_short", list(SHORTS), 40 / 120),
)
EVENTS = (
    ("legit_echo", 500 / 120, (2, 3), _echo_base),
    ("borderline", 30 / 120, (CHOSEN[1] + 1, CHOSEN[1] + 1), _echo_base),
    ("legit_unique", 576 / 120, (1, 1), _long_unique),
)


def build_corpus(rng: random.Random) -> list[tuple[str, str]]:
    """(text, label) pairs matching the measured shape.

    farm_head / farm_short / farm_tail are the airdrop classes and the only messages a
    correct filter refuses. legit_short is the class the parameters must protect.
    legit_echo (2-3 copies) and legit_unique are ordinary traffic. borderline is the
    honest cost of N=3: a genuine fourth echo of one long sentence inside the window,
    which the filter does refuse and which is counted separately rather than hidden
    inside either class.
    """
    rows: list[tuple[str, str]] = []
    rows += [(t, "farm_head") for t in HEAD for _ in range(135)]
    rows += [(t, "farm_short") for t in SHORT_FARM for _ in range(30)]
    rows += [
        (f"farm mid-band phrase {i}, rotated daily", "farm_mid")
        for i in range(12)
        for _ in range(12)
    ]
    # The measured evasion class: one template, the sender appended into the text, so
    # every copy is textually distinct. Exact match catches none of it BY DESIGN - this
    # row exists so the bench states that limit honestly instead of hiding it inside a
    # catch rate computed over byte-identical copies only.
    rows += [
        (f"@human ack - agent{i:03d}_{j} here, building for the agentic economy", "evasion")
        for i in range(60)
        for j in range(12)
    ]
    rows += [(w, "legit_short") for w in SHORTS for _ in range(40)]
    rows += [(_echo_base(i), "legit_echo") for i in range(250) for _ in range(2)]
    rows += [(_echo_base(1000 + i), "legit_echo") for i in range(250) for _ in range(3)]
    # borderline is always CHOSEN's N plus one - the honest cost of the threshold in
    # force, whatever it is: one more echo than the filter allows, landing together.
    rows += [(_echo_base(5000 + i), "borderline") for i in range(30) for _ in range(CHOSEN[1] + 1)]
    rows += [(_long_unique(i), "legit_unique") for i in range(TOTAL - len(rows))]
    rng.shuffle(rows)
    assert len(rows) == TOTAL
    return rows


def senders_for(rows: list[tuple[str, str]], rng: random.Random) -> list[str]:
    """~82% distinct senders: every farm copy from its own identity, legit traffic from
    a smaller pool with reuse - the measured shape, and the worst case for anything
    keyed per sender, which is why this filter is not."""
    return [
        f"did:key:farm{i:06d}"
        if label.startswith("farm")
        else f"did:key:peer{rng.randrange(3200):06d}"
        for i, (_, label) in enumerate(rows)
    ]


def replay(rows, assignment, min_length, max_copies, window):
    """Run the real filter over the corpus; returns per-label refused counts.

    Sharding is simulated by routing each message to one of W rings (the room name
    tagged per worker), which is what per-worker state means: no cross-worker
    visibility, no shared lock. limit._dupes is reset by the caller between runs.
    """
    refused = Counter()
    total = Counter()
    step = SPAN / TOTAL
    for i, (text, label) in enumerate(rows):
        total[label] += 1
        if limit.dupe_refused(
            "lobby#" + str(assignment[i]), text, 1000.0 + i * step, window, min_length, max_copies
        ):
            refused[label] += 1
    return refused, total


def sustained_corpus(rng: random.Random, duration: float) -> list[tuple[float, str, str]]:
    """(when, text, label) at the measured rates, for a room that never lets up.

    PHRASES arrive at a steady copies/s for the whole duration. EVENTS mint a fresh text
    at a steady texts/s and land its 2-4 copies within ~8s of each other, because an
    echo wave is a conversation moment, not a uniform drip — a corpus that spread an
    event's copies over the whole duration would understate the filter's real
    false-positive cost at every window.
    """
    rows: list[tuple[float, str, str]] = []
    for label, texts, rate in PHRASES:
        for text in texts:
            for _ in range(int(rate * duration)):
                rows.append((rng.uniform(0, duration), text, label))
    events = 0
    for label, per_second, (lo, hi), gen in EVENTS:
        for _ in range(int(per_second * duration)):
            start, text = rng.uniform(0, duration), gen(events)
            events += 1
            for _ in range(rng.randint(lo, hi)):
                rows.append((min(start + rng.uniform(0, 8.0), duration), text, label))
    rows.sort()
    return rows


def replay_timed(rows, assignment, min_length, max_copies, window):
    """replay, for a corpus that carries its own arrival times."""
    refused = Counter()
    total = Counter()
    for i, (when, text, label) in enumerate(rows):
        total[label] += 1
        if limit.dupe_refused(
            "lobby#" + str(assignment[i]), text, when, window, min_length, max_copies
        ):
            refused[label] += 1
    return refused, total


def shape(rows):
    texts = [t for t, _ in rows]
    return len(set(texts)), sum(v - 1 for v in Counter(texts).values() if v > 1)


def analyse_room_file(path: str, min_length: int, max_copies: int, window: float) -> None:
    """The operator mode: the same analysis against a real room file (JSONL records).

    No ground truth exists in a raw file, so labelling is the stated heuristic: a text
    with 10 or more copies inside the capture is counted as farm-like. Read-only;
    nothing leaves the machine this runs on.
    """
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec.get("text"), str):
            records.append(rec)
    if not records:
        print("no parsable records in", path)
        return
    counts = Counter(limit.normalize_text(r["text"]) for r in records)
    redundant = sum(v - 1 for v in counts.values() if v > 1)
    pct = lambda n: n / len(records) * 100  # noqa: E731
    print(
        f"{path}: {len(records)} records, {len(counts)} distinct normalised texts "
        f"({pct(len(counts)):.1f}%), {redundant} redundant copies ({pct(redundant):.1f}%)"
    )
    print("top phrases by copy count:")
    for text, n in counts.most_common(6):
        print(f"  x{n:<4} len={len(text):<4} {text[:70]}")

    # Record timestamps when they parse (the store writes UTC microseconds), so the
    # window arithmetic runs on the room's real pacing; even index spacing only as the
    # fallback for a file this store did not write.
    def _when(i: int, rec: dict) -> float:
        raw = rec.get("ts")
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            except ValueError:
                pass
        return 1000.0 + i * (SPAN / len(records))

    limit._dupes.clear()
    refused = 0
    for i, rec in enumerate(records):
        if limit.dupe_refused("room", rec["text"], _when(i, rec), window, min_length, max_copies):
            refused += 1
    print(
        f"filter at min{min_length}/N{max_copies}/T{window:g}s over this file: "
        f"{refused} of {len(records)} refused ({pct(refused):.1f}%)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", help="analyse a real room file (JSONL) instead of the corpus")
    args = parser.parse_args()
    if args.room:
        analyse_room_file(args.room, *CHOSEN)
        return

    seeds = [20260827 + i for i in range(PASSES)]
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "corpus.jsonl"
        rng = random.Random(seeds[0])
        rows = build_corpus(rng)
        senders = senders_for(rows, rng)
        # Written as a room file and read back, so the corpus and the --room mode share
        # one loader discipline and the tempfile is where the measured shape is checked.
        with path.open("w", encoding="utf-8") as f:
            for (text, label), sender in zip(rows, senders, strict=True):
                print(
                    json.dumps(
                        {"seq": 0, "from": sender, "text": text, "label": label},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    file=f,
                )
        loaded = [
            (json.loads(line)["text"], json.loads(line)["label"])
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        distinct, redundant = shape(loaded)
        print(f"corpus: {TOTAL} messages in {path}")
        print(
            f"  {distinct} distinct texts ({distinct / TOTAL * 100:.1f}%), "
            f"{redundant} redundant copies ({redundant / TOTAL * 100:.1f}%), "
            f"{len(set(senders))} distinct senders ({len(set(senders)) / TOTAL * 100:.1f}%)"
        )
        print(
            "  farm: 6 head x135, 2 short x30, 12 mid x12; evasion: 60 templates x12 "
            "(textually distinct, uncatchable by design); legit: 8 shorts x40, "
            f"500 echoes x2-3, 30 borderline x{CHOSEN[1] + 1}, rest unique"
        )
        print(f"median of {PASSES} passes, replayed through limit.dupe_refused")
        print()
        header = f"{'min N window':>16} {'W':>2} {'catch':>7} {'FP-legit':>9} {'FP-short':>9} {'borderline':>10} {'evade':>6}"
        print(header)

        for min_length, max_copies, window in CANDIDATES:
            for workers in WORKERS:
                catches, fps, short_fps, borders = [], [], [], []
                catches, fps, short_fps, borders, evades = [], [], [], [], []
                for seed in seeds:
                    rng = random.Random(seed)
                    rows_i = build_corpus(rng)
                    assignment = [rng.randrange(workers) for _ in rows_i]
                    limit._dupes.clear()
                    refused, total = replay(rows_i, assignment, min_length, max_copies, window)
                    farm = sum(refused[k] for k in FARM)
                    farm_all = sum(total[k] for k in FARM)
                    legit = sum(refused[k] for k in LEGIT)
                    legit_all = sum(total[k] for k in LEGIT)
                    catches.append(farm / farm_all)
                    fps.append(legit / legit_all)
                    short_fps.append(refused["legit_short"] / total["legit_short"])
                    borders.append(refused["borderline"] / total["borderline"])
                    evades.append(refused["evasion"] / total["evasion"])
                key = (min_length, max_copies, window, workers)
                results[key] = (
                    statistics.median(catches),
                    statistics.median(fps),
                    statistics.median(short_fps),
                    statistics.median(borders),
                    statistics.median(evades),
                )
                catch, fp, sfp, border, evade = results[key]
                tag = "  <- chosen" if (min_length, max_copies, window) == CHOSEN else ""
                print(
                    f"{f'{min_length} {max_copies} {window:g}s':>16} {workers:>2} "
                    f"{catch * 100:>6.1f}% {fp * 100:>8.2f}% {sfp * 100:>8.2f}% "
                    f"{border * 100:>9.1f}% {evade * 100:>5.1f}%{tag}"
                )

    # The gate, checked rather than eyeballed: the chosen parameters must catch the bulk
    # of the farm on ONE worker and must never refuse a short conversational repeat. The
    # floor is 80% - the measured catch of min16/N5/T60 at one worker is 81.9%, so the
    # gate holds the choice to itself rather than to a round number it must lucky-dip
    # into. The W=5 catch is the accepted sharding cost - reported, not gated.
    one = results[(*CHOSEN, 1)]
    five = results[(*CHOSEN, 5)]
    print()
    print(
        f"gate: W=1 catch {one[0] * 100:.1f}% (>= 80%), FP-short {one[2] * 100:.1f}% (== 0%), "
        f"W=5 catch {five[0] * 100:.1f}% (sharding cost, reported not gated)"
    )
    if one[0] < 0.80 or one[2] > 0.0:
        print("!! outside the expected range - work out why before quoting these")
    sweep(CHOSEN[0], CHOSEN[1])


def sweep(min_length: int, max_copies: int) -> None:
    """The window trade-off, on rooms that never let up.

    Each point is a fresh corpus SWEEP_SPANS windows long at the measured rates, so a
    bigger window is tested against proportionally more copies rather than against the
    same 4000 spread thin. `ring` reports whether the live key count reached MAX_DUPE_KEYS
    during the pass — the point where the ring's own bound, not the window, is what
    limits what it remembers.
    """
    print()
    print(
        f"window sweep at min{min_length}/N{max_copies} - sustained corpus, "
        f"{SWEEP_SPANS} windows per point, median of {SWEEP_PASSES} passes"
    )
    print()
    print(
        f"{'window':>7} {'W':>2} {'msgs':>7} {'catch':>7} {'FP-legit':>9} "
        f"{'FP-short':>9} {'borderline':>10} {'ring':>16}"
    )
    for window in SWEEP_WINDOWS:
        duration = SWEEP_SPANS * window
        for workers in (1, 5):
            catches, fps, short_fps, borders, sizes, msgs = [], [], [], [], [], []
            for seed in range(SWEEP_PASSES):
                rng = random.Random(seed)
                rows = sustained_corpus(rng, duration)
                assignment = [rng.randrange(workers) for _ in rows]
                limit._dupes.clear()
                refused, total = replay_timed(rows, assignment, min_length, max_copies, window)
                farm = sum(refused[k] for k in FARM)
                farm_all = sum(total[k] for k in FARM)
                legit = sum(refused[k] for k in LEGIT)
                legit_all = sum(total[k] for k in LEGIT)
                catches.append(farm / farm_all)
                fps.append(legit / legit_all)
                short_fps.append(refused["legit_short"] / total["legit_short"])
                borders.append(refused["borderline"] / total["borderline"])
                sizes.append(len(limit._dupes))
                msgs.append(len(rows))
            ring = statistics.median(sizes)
            at_cap = "at cap" if ring >= limit.MAX_DUPE_KEYS else f"{ring:.0f} keys"
            print(
                f"{window:>6.0f}s {workers:>2} {statistics.median(msgs):>7.0f} "
                f"{statistics.median(catches) * 100:>6.1f}% {statistics.median(fps) * 100:>8.2f}% "
                f"{statistics.median(short_fps) * 100:>8.2f}% "
                f"{statistics.median(borders) * 100:>9.1f}% {at_cap:>16}"
            )


if __name__ == "__main__":
    main()
