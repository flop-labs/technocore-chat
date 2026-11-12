"""Run: uv run --group dev python -m pytest tests

The duplicate ring's own rules, tested against limit.dupe_refused directly. What
matters under load is not the verdict on one message but the bounds: a ring that OOMs
is not a filter, a sweep that empties the whole map in one call is a pause, and a
refusal that records a timestamp is a window a farm can hold open forever.

Every call passes the whole parameter set explicitly - window, floor, threshold, cap -
rather than leaning on the signature defaults (which mirror the shipped config and
have moved with it). A ring test asserts arithmetic at numbers it chose.
"""

from __future__ import annotations

import itertools
import sys
import threading
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import app  # noqa: E402
import limit  # noqa: E402
import store  # noqa: E402

# The values under test, chosen here: 60s window, 16-char floor, fifth copy allowed
# (sixth refused). Deliberate numbers, not echoes of limit.py's defaults.
WINDOW = 60
FLOOR = 16
COPIES = 5
LONG = "one more copy of this sentence than allowed is refused, measured"


def refused(
    text: str,
    now: float,
    room: str = "r",
    *,
    window: float = WINDOW,
    min_length: int = FLOOR,
    max_copies: int = COPIES,
    cap: int = limit.MAX_DUPE_KEYS,
) -> bool:
    """dupe_refused with the values under test, overridable per call the way a test
    varies one knob at a time."""
    return limit.dupe_refused(room, text, now, window, min_length, max_copies, cap)


def test_normalisation_folds_case_whitespace_and_unicode_compatibility() -> None:
    """One key per meaning: NFKC first (compatibility forms decompose before casefold),
    then the store's invisible categories to spaces, then casefold, then whitespace
    collapse. Trailing punctuation stays a difference on purpose - measured, stripping it
    catches nothing."""
    a = limit.normalize_text("Checking   Node HEALTH... all good")
    b = limit.normalize_text("checking node health... all good")
    full = limit.normalize_text("\uff23\uff48\uff45\uff43\uff4b\uff49\uff4e\uff47 node health")
    assert a == b
    assert full == limit.normalize_text("checking node health")
    # A zero-width joiner and a line separator are invisible to the store and to this.
    assert limit.normalize_text("con\u200dsensus\u2028node") == limit.normalize_text(
        "con sensus node"
    )


def test_the_length_floor_is_on_the_normalised_text() -> None:
    """The floor is the boundary: at or above it the filter applies, below it never
    does - the entire conversational-repeat class lives below it."""
    limit._dupes.clear()
    limit._rings.clear()
    assert refused("x" * (FLOOR - 1), now=0.0) is False
    assert not limit._dupes, "a short text must not even be recorded"
    for i in range(COPIES + 3):
        assert refused("x" * FLOOR, now=float(i)) is (i >= COPIES)


def test_a_refusal_never_extends_the_window() -> None:
    """Only accepts are recorded. A farm hammering the refusal must not push the
    expiry out: the phrase opens again exactly 'window' after the last copy that
    landed, which is what makes the filter survivable to run at all."""
    limit._dupes.clear()
    limit._rings.clear()
    for i in range(COPIES):
        assert refused(LONG, now=100.0 + i) is False
    for t in range(100 + COPIES, 160):
        assert refused(LONG, now=float(t)) is True
    # 100+COPIES-1 was the last accept; WINDOW after it the window is shut on it,
    # refusals notwithstanding.
    assert refused(LONG, now=100.0 + COPIES - 1 + WINDOW + 0.1) is False


def test_the_threshold_decides_which_copy_is_the_refused_one() -> None:
    """COPIES is arithmetic, not a constant of nature: at 2 the third copy is refused.
    Pinned so a retune of the default cannot silently re-tune what this file means."""
    limit._dupes.clear()
    limit._rings.clear()
    assert refused(LONG, now=0.0, max_copies=2) is False
    assert refused(LONG, now=1.0, max_copies=2) is False
    assert refused(LONG, now=2.0, max_copies=2) is True


def test_the_ring_stays_bounded_under_a_flood() -> None:
    """The bound that matters under load. Every key here is live - nothing has expired,
    so only the hard cap holds the line, and what survives is the newest."""
    limit._dupes.clear()
    limit._rings.clear()
    cap = 128
    for i in range(20_000):
        refused("phrase number " + str(i), now=1000.0, window=300.0, cap=cap)
    assert len(limit._dupes) == cap
    assert refused("phrase number 19999", now=1000.0, window=300.0, cap=cap) is False
    limit._dupes.clear()
    limit._rings.clear()


def test_one_write_never_pays_for_the_whole_backlog() -> None:
    """The sweep is capped per call: a burst of expiry cannot turn one accepted write
    into a pause that holds the very lock-free path this filter protects."""
    limit._dupes.clear()
    limit._rings.clear()
    for i in range(1000):
        refused("an old phrase number " + str(i), now=0.0, window=1.0, cap=10_000)
    before = len(limit._dupes)
    refused("a fresh phrase indeed", now=500.0, window=1.0, cap=10_000)
    assert len(limit._dupes) == before - 8 + 1
    limit._dupes.clear()
    limit._rings.clear()


def test_off_is_one_comparison_and_touches_nothing() -> None:
    """window=0 is the opt-out, so it must cost nothing and record nothing - an operator
    setting CHAT_DUPE_FILTER_SECONDS=0 buys back the pre-filter hot path exactly."""
    limit._dupes.clear()
    limit._rings.clear()
    assert refused(LONG, now=0.0, window=0) is False
    assert not limit._dupes
    # The window's 0 is the ONE opt-out: it short-circuits before the key is built, so it
    # takes the share cap's ring with it - config.py says so, and this is what says it.
    assert not limit._rings


def test_rooms_are_isolated_and_copies_count_not_senders() -> None:
    """The key has no sender in it - that is the whole point of a cross-sender filter -
    and it has a room in it, so two rooms can hold the same conversation independently."""
    limit._dupes.clear()
    limit._rings.clear()
    for i in range(COPIES):
        for room in ("lobby", "meta"):
            assert refused(LONG, now=float(i), room=room) is False
    assert refused(LONG, now=float(COPIES), room="lobby") is True
    assert refused(LONG, now=float(COPIES), room="meta") is True
    assert refused(LONG, now=float(COPIES), room="elsewhere") is False
    limit._dupes.clear()
    limit._rings.clear()


def test_releasing_a_reserved_copy_gives_exactly_that_slot_back() -> None:
    """A copy is reserved before the append, and the append has refusals of its own.
    Releasing has to give back the one timestamp it reserved - not the key, not the
    whole window - or a write the store rejected either spends a slot forever or wipes
    copies that did land."""
    limit._dupes.clear()
    limit._rings.clear()
    for i in range(COPIES):
        assert refused(LONG, now=float(i)) is False
    limit.dupe_release("r", LONG, 4.0, WINDOW, FLOOR)
    assert refused(LONG, now=5.0) is False, "the released slot is the one just taken"
    assert refused(LONG, now=6.0) is True, "and only that one - the other four still count"
    # Releasing the last live copy drops the key rather than leaving an empty tuple to
    # be swept later: the ring's bound is keys, not timestamps.
    for reserved in (0.0, 1.0, 2.0, 3.0, 5.0):
        limit.dupe_release("r", LONG, reserved, WINDOW, FLOOR)
    assert not limit._dupes


def test_releasing_what_was_never_reserved_is_silent() -> None:
    """The release runs on the failure path, where the reservation may already have been
    swept, evicted, or never taken at all (an off filter, a text under the floor). None
    of those may raise: the caller is already returning an error the store chose."""
    limit._dupes.clear()
    limit._rings.clear()
    limit.dupe_release("r", LONG, 1.0, WINDOW, FLOOR)  # never reserved
    limit.dupe_release("r", "x" * (FLOOR - 1), 1.0, WINDOW, FLOOR)  # under the floor
    limit.dupe_release("r", LONG, 1.0, 0, FLOOR)  # filter off
    assert not limit._dupes


def test_a_stale_release_cannot_wipe_a_later_successful_copy() -> None:
    """The reviewer's race in #734. A reservation is taken, its ring slot is evicted by a
    ring's worth of other filterable messages, then the SAME text lands again and sticks -
    and only now does the original reservation's write fail and release. That release runs
    at the original 'now', and must not remove the later, successful slot: bare digests let
    it (every copy of a digest was interchangeable), the (instant, digest) pair does not."""
    limit._dupes.clear()
    limit._rings.clear()
    room = "r"
    key = limit._dupe_key(room, LONG, FLOOR)
    assert key is not None
    digest = key[1]

    def ring_count() -> int:
        return sum(d == digest for _, d in limit._rings.get((room, 0), ()))

    assert refused(LONG, now=0.0) is False  # reserve X at now=0
    for i in range(limit.DUPE_RING):  # a full ring of distinct texts evicts X's slot
        assert refused("distinct filler phrase number " + str(i), now=1.0) is False
    assert ring_count() == 0, "X's original slot has been pushed out of the ring"
    assert refused(LONG, now=float(WINDOW + 1)) is False  # X lands again, and this one sticks
    assert ring_count() == 1
    limit.dupe_release(room, LONG, 0.0, WINDOW, FLOOR)  # the ORIGINAL reservation, now stale
    assert ring_count() == 1, "the later successful copy must survive"
    limit._dupes.clear()
    limit._rings.clear()


def test_concurrent_writers_never_corrupt_the_ring() -> None:
    """Every write lane reaches the ring from a threadpool - the GET lanes are sync
    endpoints, the POST goes through run_in_threadpool - so the check, the record, the
    sweep and the eviction have to be one atomic step.

    Unguarded they were not: the sweep's walk from the front raced an insert into
    'OrderedDict mutated during iteration', and its delete raced another thread's
    eviction into a KeyError - a 500 on exactly the write path this filter exists to
    protect, reached by a flood of DISTINCT texts, which is what an evasive farm sends.
    A small cap and a one-second window keep every call inside both loops, which is
    where the race lives; the switch interval makes the interleaving reliable rather
    than lucky.
    """
    limit._dupes.clear()
    limit._rings.clear()
    cap, errors, counter = 64, [], itertools.count()

    def flood() -> None:
        try:
            for _ in range(2_000):
                n = next(counter)
                refused("a distinct phrase number " + str(n), now=float(n % 3), window=1.0, cap=cap)
        except BaseException as exc:  # noqa: BLE001 - the exception IS what this asserts on
            errors.append(exc)

    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=flood) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(switch)
    assert not errors, [repr(exc) for exc in errors[:3]]
    assert len(limit._dupes) <= cap, "the bound has to hold under concurrency too"
    limit._dupes.clear()
    limit._rings.clear()


# --------------------------------------------------------- the sweep rung, exhaustively
#
# normalize_text carries a sweep rung it does not obviously need, because store.append
# sweeps too. Its docstring gives the reason: "the unsigned lanes reach this BEFORE
# store.append runs clean_text - keying the unswept bytes there and the swept bytes on the
# signed lane would make one text two keys." That is true of the code as written -
# room_say reserves with the raw path text, room_say_signed with clean_text(text) - so the
# rung is load-bearing for a property no single-lane test can see, and the two below are
# the differential check on it.
#
# Both are exhaustive rather than exemplary on purpose. The rung reconciles two orderings
# (NFKC-then-sweep against sweep-then-NFKC-then-sweep) and whether they agree is a fact
# about the Unicode tables, not about this file: it can stop being true with no commit
# here at all, which is precisely the drift a handful of examples does not catch.


def _one_char_probe(char: str) -> str:
    """A text long enough to reach the ring, differing from its neighbours in one char.

    The padding is plain ASCII so it can never be the thing that differs, and it is long
    enough that the normalised form clears any floor a caller might set (21 characters
    against a 16-character floor), because a probe the floor exempts asserts nothing.
    """
    return "duplicate-text-" + char + "-tail"


def test_a_pre_swept_text_keys_to_the_same_slot_as_the_raw_one() -> None:
    """The signed lanes hand the ring text that clean_text has already swept; the
    unsigned lanes hand it the raw bytes. One text must not become two slots, or a caller
    alternating lanes buys max_copies again per lane and the filter's threshold is a
    quarter of what it says.

    Checked over every code point either transform touches - 144,681 of them: the
    invisible categories plus everything NFKC rewrites. Outside that set clean_text and
    NFKC are both the identity on the character, so the two sides are the same expression
    and there is nothing left to compare.
    """
    active = [
        cp
        for cp in range(0x110000)
        if unicodedata.category(chr(cp)) in store.INVISIBLE_CATEGORIES
        or unicodedata.normalize("NFKC", chr(cp)) != chr(cp)
    ]
    assert len(active) > 100_000, f"only {len(active)} code points selected; the filter is wrong"

    divergent = []
    for cp in active:
        raw = _one_char_probe(chr(cp))
        if limit.normalize_text(raw) != limit.normalize_text(store.clean_text(raw)):
            divergent.append(cp)

    assert not divergent, (
        f"{len(divergent)} code points key differently depending on whether the lane swept "
        f"first, e.g. U+{divergent[0]:04X}: the signed and unsigned lanes would take one "
        f"ring slot each for one text, so alternating them doubles a sender's copy budget"
    )


def test_nfkc_moves_no_character_across_the_swept_boundary() -> None:
    """Why the test above passes, pinned separately because it is a property of
    unicodedata and not of this repo.

    Sweeping after NFKC agrees with sweeping before it only while NFKC never rewrites a
    visible character into an invisible one, or the reverse. Nothing in the standard
    promises that: a future table could give some format character a compatibility
    decomposition and silently split one text into two ring keys. Asserting it here means
    that arrives as a red test naming the character, rather than as a filter quietly
    catching half of what it reports.

    Holds on unicodedata 15.0.0 and 15.1.0, measured.
    """
    crossings = []
    for cp in range(0x110000):
        char = chr(cp)
        was_swept = unicodedata.category(char) in store.INVISIBLE_CATEGORIES
        folded = unicodedata.normalize("NFKC", char)
        # "Invisible after folding" means every character of the decomposition is swept:
        # that is what decides whether the sweep can still see it.
        now_swept = bool(folded) and all(
            unicodedata.category(c) in store.INVISIBLE_CATEGORIES for c in folded
        )
        if was_swept != now_swept:
            crossings.append(cp)

    assert not crossings, (
        f"NFKC crosses the swept boundary at {len(crossings)} code points, e.g. "
        f"U+{crossings[0]:04X} ({unicodedata.category(chr(crossings[0]))}): normalize_text "
        f"sweeps after folding and clean_text sweeps before, so the two now disagree"
    )


# ----------------------------------------------------------------- the share cap
#
# The window's own rules are above. These are the second signal's: a room's ring of
# recently accepted keys, and the share of it one text may hold. Its bounds matter for
# the same reason the window ring's do - it is per-room state on a world-writable
# service - and its release matters for the same reason dupe_release does.


def test_one_text_may_hold_only_its_share_of_a_rooms_ring() -> None:
    """The window-free half, as arithmetic. Every copy here lands a full window after the
    one before it, so the timestamp rule can never be what refuses - and the cap still
    arrives, at DUPE_SHARE of DUPE_RING slots, because it counts composition rather than
    rate. This is the shape of issue #697: a repeater that sleeps past the window."""
    limit._dupes.clear()
    limit._rings.clear()
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    for i in range(allowed):
        assert refused(LONG, now=float(i) * (WINDOW + 1)) is False, i
    assert refused(LONG, now=float(allowed) * (WINDOW + 1)) is True
    # Per text, not a room-wide gate: the room is still open to everything else.
    assert refused(LONG + " with a real answer bolted on", now=float(allowed)) is False
    # And per room, like every other key here.
    assert refused(LONG, now=float(allowed) * (WINDOW + 1), room="elsewhere") is False
    limit._dupes.clear()
    limit._rings.clear()


def test_a_share_cap_refusal_never_extends_the_ring() -> None:
    """The ring's half of "only accepts are recorded", pinned separately because the
    share cap refuses on its own path, above the window's. A copy the cap turns away must
    leave the room's slots exactly as it found them: the way back from this rule is other
    messages pushing the copies out, and a refusal that took a slot of its own would keep
    the repeater's own share topped up for as long as it kept hammering."""
    limit._dupes.clear()
    limit._rings.clear()
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    for i in range(allowed):
        assert refused(LONG, now=float(i) * (WINDOW + 1)) is False, i
    before = limit._rings[("r", 0)]
    assert len(before) == allowed
    # Every one of these is refused by the SHARE CAP and not by the window: a full window
    # has passed since the copy before it, so no timestamp of this text is live.
    for i in range(20):
        assert refused(LONG, now=float(allowed + i) * (WINDOW + 1)) is True, i
    assert limit._rings[("r", 0)] == before, "a share-cap refusal records no slot"
    limit._dupes.clear()
    limit._rings.clear()


def test_the_share_ring_is_bounded_per_room_and_across_rooms() -> None:
    """A second structure needs the second bound: DUPE_RING entries per room, and
    MAX_RING_ROOMS rooms LRU-evicted, so the whole thing is a fixed 32k digests whatever a
    caller posting one long text to every room it can name does to it.

    LRU and not insertion order, which is why one room is deliberately re-touched in the
    middle: filling in creation order alone makes the two indistinguishable, and the
    refresh-on-write is exactly the thing that keeps a busy old room from being evicted
    out from under a repeater it is still holding."""
    limit._dupes.clear()
    limit._rings.clear()
    for i in range(limit.MAX_RING_ROOMS):
        assert (
            refused("a phrase long enough to be filtered", now=1000.0, room="r" + str(i)) is False
        )
    assert len(limit._rings) == limit.MAX_RING_ROOMS
    # r0 and r1 are the same age. This write is the only difference between them, and it
    # has to be enough: use, not creation, is what the eviction order means.
    assert refused("another phrase long enough to be filtered", now=1000.0, room="r0") is False
    for i in range(100):
        assert (
            refused("a phrase long enough to be filtered", now=1000.0, room="new" + str(i)) is False
        )
    assert len(limit._rings) == limit.MAX_RING_ROOMS
    assert ("r0", 0) in limit._rings, "the re-touched room is the young one now"
    assert ("r1", 0) not in limit._rings, "and the idlest room, its exact contemporary, is evicted"
    assert ("new99", 0) in limit._rings, "and the newest survives"

    limit._rings.clear()
    for i in range(limit.DUPE_RING * 3):
        refused("a distinct phrase number " + str(i), now=1000.0, room="one")
    assert len(limit._rings[("one", 0)]) == limit.DUPE_RING
    limit._dupes.clear()
    limit._rings.clear()


def test_a_share_capped_room_survives_eviction_while_under_attack() -> None:
    """Minh3132's #734 finding: a refused write is an access, and the ring's LRU has to
    treat it as one. Fill a room to the share cap, then admit a filterable message in more
    than MAX_RING_ROOMS other rooms while hammering the capped room with copies the SHARE
    CAP alone turns away. The capped room is older by creation than every flood room, so
    only its refused touches can keep it resident - and they must. Before the fix the ring
    was refreshed only by an ACCEPTED write, so a room whose traffic is all refusals looked
    idle, got evicted, and its next copy met an empty ring and rebuilt a fresh cap's worth
    of allowance - reopening exactly the slow-repetition this filter exists to bound."""
    limit._dupes.clear()
    limit._rings.clear()
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    for i in range(allowed):
        assert refused(LONG, now=float(i) * (WINDOW + 1), room="hot") is False, i
    assert refused(LONG, now=float(allowed) * (WINDOW + 1), room="hot") is True
    contents = limit._rings[("hot", 0)]
    for i in range(limit.MAX_RING_ROOMS + 50):
        # A share-cap refusal in the capped room, each spaced past the window so ONLY the
        # cap can be refusing, then one accepted message in a brand-new room that pushes the
        # LRU front along. Without refresh-on-refusal "hot" is the oldest key and is evicted.
        assert refused(LONG, now=float(allowed + 1 + i) * (WINDOW + 1), room="hot") is True, i
        assert (
            refused("a phrase long enough to be filtered", now=1000.0, room="new" + str(i)) is False
        )
    assert ("hot", 0) in limit._rings, "the room under active attack must survive its own flood"
    assert limit._rings[("hot", 0)] == contents, "and a share-cap refusal still records no slot"
    assert refused(LONG, now=1e9, room="hot") is True, "so the next copy is still refused"
    assert len(limit._rings) == limit.MAX_RING_ROOMS
    assert ("new0", 0) not in limit._rings, "while an idle flood room is the one evicted"
    limit._dupes.clear()
    limit._rings.clear()


def test_a_window_hammered_room_survives_eviction_too() -> None:
    """The same finding by its other door, which is what makes the fix the root and not a
    patch on one exit. A room refused by the WINDOW rule (copies inside the window, well
    under the share cap) also returns above the accepted write that used to be the ring's
    only LRU refresh, so its ring is evictable while under active attack in exactly the way
    a share-capped room's is. Refresh-on-refusal has to cover both refusal rules at once, or
    a fix that touched only the share-cap exit would leave this path as the next finding."""
    limit._dupes.clear()
    limit._rings.clear()
    # COPIES accepted copies at one instant give "hot" both a ring and a full window; every
    # further copy at that same instant is a WINDOW refusal (COPIES is far under the share
    # cap of 32), and since a refusal adds no timestamp the window stays full while `now`
    # holds - so the whole flood below is real window refusals, not a decayed rate.
    for i in range(COPIES):
        assert refused(LONG, now=1000.0, room="hot") is False, i
    assert refused(LONG, now=1000.0, room="hot") is True, "the sixth copy trips the window"
    contents = limit._rings[("hot", 0)]
    assert len(contents) == COPIES, "the accepted copies are the room's whole ring"
    for i in range(limit.MAX_RING_ROOMS + 50):
        assert refused(LONG, now=1000.0, room="hot") is True, i  # window refusal, not the cap
        assert (
            refused("a phrase long enough to be filtered", now=1000.0, room="new" + str(i)) is False
        )
    assert ("hot", 0) in limit._rings, "a window-refused room is under attack and must survive"
    assert limit._rings[("hot", 0)] == contents, "and its ring is untouched by the refusals"
    assert len(limit._rings) == limit.MAX_RING_ROOMS
    assert ("new0", 0) not in limit._rings, "the evicted room is an idle one, not the attacked one"
    limit._dupes.clear()
    limit._rings.clear()


def test_releasing_a_reserved_copy_gives_back_its_ring_slot_too() -> None:
    """The share ring is reserved before the append exactly as the window's timestamp is,
    and the append refuses writes of its own. A slot that no write ever used has to come
    back, or DUPE_SHARE * DUPE_RING malformed requests would leave the next well-formed
    caller of that phrase refused against copies that do not exist - and the share cap,
    like everything else keyed here, has no sender in it, so that caller is anyone."""
    limit._dupes.clear()
    limit._rings.clear()
    allowed = int(limit.DUPE_SHARE * limit.DUPE_RING)
    for i in range(allowed + 3):
        stamp = float(i) * (WINDOW + 1)
        assert refused(LONG, now=stamp) is False
        limit.dupe_release("r", LONG, stamp, WINDOW, FLOOR)
    # Dropped, not left as an empty tuple: release re-inserts only what it has something
    # to put back, so dupe_refused stays the only path that grows either map - which is
    # what makes its in-lock trim the whole story about their bounds.
    assert not limit._rings.get(("r", 0)), "every slot reserved was handed back"
    assert ("r", 0) not in limit._rings, "and an emptied room leaves no key behind"
    assert refused(LONG, now=1e6) is False, "and the phrase is still free to land"
    # Exactly the slot reserved, not the room's ring: another text's slot survives it.
    other = LONG + " and a different tail"
    assert refused(other, now=1e6 + 1) is False
    limit.dupe_release("r", LONG, 1e6, WINDOW, FLOOR)
    key = limit._dupe_key("r", other, FLOOR)
    assert key is not None and limit._rings[("r", 0)] == ((1e6 + 1, key[1]),)
    limit._dupes.clear()
    limit._rings.clear()


def test_releasing_one_of_two_copies_reserved_at_the_same_instant_frees_one_slot() -> None:
    """Two threads reserving the same text can be handed the same time.monotonic() float -
    rare, and reachable, because the clock's resolution is not the lock's.

    The ring stores (instant, digest) pairs so a stale release matches only its own slot
    (test_a_stale_release_cannot_wipe_a_later_successful_copy), and a collision on the
    instant makes two slots that are byte-identical. That is safe only because release
    removes ONE match with `list.remove`, not every equal entry: an earlier draft filtered
    on the pair and dropped BOTH slots here, handing back a slot the other reservation was
    still holding. One failed write must free exactly one slot.

    Asserted on both structures, since the window's tuple has the same collision.
    """
    limit._dupes.clear()
    limit._rings.clear()
    key = limit._dupe_key("r", LONG, FLOOR)
    assert key is not None
    assert refused(LONG, now=7.0) is False
    assert refused(LONG, now=7.0) is False, "two reservations, one instant"
    assert limit._rings[("r", 0)] == ((7.0, key[1]), (7.0, key[1]))
    limit.dupe_release("r", LONG, 7.0, WINDOW, FLOOR)  # only one of the two failed
    assert limit._rings[("r", 0)] == ((7.0, key[1]),), "one released slot, not both"
    assert limit._dupes[key] == (7.0,), "and the window's copy count agrees"
    limit._dupes.clear()
    limit._rings.clear()


def test_concurrent_writers_never_corrupt_the_share_ring() -> None:
    """The ring is written in the same critical section as the window's map, so it
    inherits that lock - but nothing asserted its bounds actually survive contention.

    Both bounds are load-bearing and both are trimmed inside the lock: DUPE_RING entries
    per room (a tuple rebuilt and re-sliced on every accepted write, which is a
    read-modify-write a lost update would silently overgrow) and MAX_RING_ROOMS rooms
    LRU-evicted from the front while other threads insert - the same
    `OrderedDict mutated during iteration`/KeyError shape the window's map has. Distinct
    texts across many more rooms than the cap, so every call reaches both trims; the
    switch interval makes the interleaving reliable rather than lucky.
    """
    limit._dupes.clear()
    limit._rings.clear()
    rooms, errors, counter = limit.MAX_RING_ROOMS + 200, [], itertools.count()

    def flood() -> None:
        try:
            for _ in range(2_000):
                n = next(counter)
                refused(
                    "a distinct phrase number " + str(n % 97),
                    now=1000.0,
                    room="r" + str(n % rooms),
                    window=300.0,
                )
        except BaseException as exc:  # noqa: BLE001 - the exception IS what this asserts on
            errors.append(exc)

    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=flood) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(switch)
    assert not errors, [repr(exc) for exc in errors[:3]]
    assert len(limit._rings) <= limit.MAX_RING_ROOMS, "the room bound holds under contention"
    assert all(len(ring) <= limit.DUPE_RING for ring in limit._rings.values()), (
        "and so does the per-room bound, which is a rebuild every writer races on"
    )
    limit._dupes.clear()
    limit._rings.clear()


def test_the_sweep_survives_an_entry_a_direct_caller_can_leave_empty() -> None:
    """`max_copies` is a plain parameter, and the sweep's predicate has to be total over
    whatever a caller can put in the map - not just over what the shipped config produces.

    config.py floors CHAT_DUPE_MAX_COPIES at 1, so nothing in the service stores an empty
    tuple. A direct caller passing 0 does: the refusal returns before a timestamp is
    added, and `live[-0:]` is the whole (empty) tuple. The sweep then meets it as the
    oldest key. `max(())` RAISES there where `all(())` did not, which would turn a
    parameter this function accepts into a 500 on the write path - so the predicate is
    written to be total, and this pins that rather than the reachability argument, which
    is one config edit away from being wrong.
    """
    limit._dupes.clear()
    limit._rings.clear()
    key = limit._dupe_key("r", LONG, FLOOR)
    assert key is not None
    assert refused(LONG, now=0.0, max_copies=0) is True, "0 refuses the first copy"
    assert limit._dupes[key] == (), "and leaves the empty entry the sweep has to survive"
    # A later write whose sweep walks from the front and meets it: no raise, and it goes.
    assert refused("a different phrase entirely here", now=1000.0, window=1.0) is False
    assert key not in limit._dupes, "an entry with nothing live in it is swept, not raised on"
    limit._dupes.clear()
    limit._rings.clear()


# The reservation instant (yukkie3276, #734 finding #8): _DupeReserver used to capture
# time.monotonic() at CONSTRUCTION, before store.append takes the room lock, and stamp the
# accepted copy with it. A write that waited out the whole window on the lock landed now but
# was recorded in the past, so the next identical copy pruned it as expired and walked through
# the dupe_max_copies/window cap. The instant must be sampled INSIDE reserve(), under the lock.
# These drive the real app._DupeReserver with a controlled clock standing in for monotonic time.
def _reserver_knobs(monkeypatch, clock) -> None:
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app, "DUPE_FILTER_SECONDS", WINDOW)
    monkeypatch.setattr(app, "DUPE_MIN_LENGTH", FLOOR)
    monkeypatch.setattr(app, "DUPE_MAX_COPIES", COPIES)


def test_the_window_instant_is_sampled_at_reserve_not_construction(monkeypatch) -> None:
    """The finding itself. Construct the reserver, then advance the clock a full window before
    reserve() runs - the lock wait - and the copy must be stamped at reserve time, not at the
    instant it was queued. Before the fix this recorded 100.0; after it records the later one."""
    limit._dupes.clear()
    limit._rings.clear()
    clock = [100.0]
    _reserver_knobs(monkeypatch, clock)
    reserver = app._DupeReserver("r", LONG)  # constructed at clock=100
    clock[0] = 100.0 + WINDOW + 5  # a full-window wait on the room lock before reserve()
    assert reserver.reserve(1) is False, "the first copy lands"
    key = limit._dupe_key("r", LONG, FLOOR)
    assert key is not None
    assert limit._dupes[key] == (100.0 + WINDOW + 5,), "stamped when it landed, not when it queued"
    limit._dupes.clear()
    limit._rings.clear()


def test_a_copy_after_a_long_lock_wait_still_counts_against_the_window(monkeypatch) -> None:
    """The contract the under-lock instant protects. One copy is delayed a full window on the
    lock, then COPIES-1 more land a second apart - all within one window of the first's LANDING.
    The copy past the cap is refused by the window. Before the fix the delayed copy was stamped
    in the past, pruned by the later ones, and the count never reached the cap (a silent bypass);
    the share ring (cap 32) is nowhere near, so only the window can be the one refusing here."""
    limit._dupes.clear()
    limit._rings.clear()
    clock = [100.0]
    _reserver_knobs(monkeypatch, clock)
    r0 = app._DupeReserver("r", LONG)  # queued at 100
    clock[0] = 100.0 + WINDOW + 5  # landed a full window later
    assert r0.reserve(1) is False
    for i in range(1, COPIES):  # COPIES-1 more, a second apart, all within a window of r0's landing
        clock[0] = 100.0 + WINDOW + 5 + i
        assert app._DupeReserver("r", LONG).reserve(1) is False, i
    clock[0] = 100.0 + WINDOW + 5 + COPIES
    assert app._DupeReserver("r", LONG).reserve(1) is True, "the window cap fires across the wait"
    limit._dupes.clear()
    limit._rings.clear()


def test_release_matches_a_slot_reserved_under_the_lock(monkeypatch) -> None:
    """Release identity rides the same under-lock instant the copy was dated with. A reserve at
    T records a window entry and a ring slot at T; a second successful copy at T' owns its own
    slot, and releasing the first gives back ONLY (T, digest) - the later copy's slot survives,
    the stale-release guarantee now exercised through the reserver with an under-lock instant."""
    limit._dupes.clear()
    limit._rings.clear()
    clock = [100.0]
    _reserver_knobs(monkeypatch, clock)
    key = limit._dupe_key("r", LONG, FLOOR)
    assert key is not None
    r1 = app._DupeReserver("r", LONG)
    clock[0] = 150.0
    assert r1.reserve(1) is False
    r2 = app._DupeReserver("r", LONG)
    clock[0] = 151.0
    assert r2.reserve(1) is False
    r1.release()
    assert limit._dupes[key] == (151.0,), "the release took only its own window stamp"
    assert limit._rings[("r", 1)] == ((151.0, key[1]),), "and only its own ring slot"
    limit._dupes.clear()
    limit._rings.clear()
