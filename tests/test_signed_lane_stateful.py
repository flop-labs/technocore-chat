"""A Hypothesis state machine over the signed lane's replay defence.

Run: uv run --group dev python -m pytest tests/test_signed_lane_stateful.py

`tests/test_store_stateful.py` models the store's lifecycle — append, read, expire, compact,
reap, note CAS — and never touches a signature. This models the part of the store whose
correctness is a *security* property: `_last_nonce`, which decides whether a captured signed
URL still works.

The contract is bounded by the ring. A signed URL is a bearer token for exactly one message,
so replaying it must fail while the message it wrote is still there to be seen; once the ring
drops that record the replay is accepted again as a fresh message, and `_last_nonce`'s
docstring says so. The bound is the retention model doing what it says rather than a hole.

This file was written against a narrower version of that contract, where the guard scanned the
newest `READ_BUDGET` rather than the whole ring, and four of its tests asserted that narrower
boundary. They are inverted rather than removed, and the paragraphs below record what moved.

WHY THE BOUND IS WHERE IT IS, which used to be somewhere else
-------------------------------------------------------------
"Refused while the original is readable" was not established by anything in `_write_record`. It
held because of a coincidence of two default arguments:

    read_messages    for raw in reverse_lines(f):        <- default max_bytes
    _last_nonce      for raw in reverse_lines(f):        <- default max_bytes

Those were the only two call sites in the module that took `reverse_lines`' default budget, and
the coincidence was doing load-bearing work nothing recorded. `_last_nonce` now names
`MAX_ROOM_BYTES` explicitly, so the guard's reach is a decision rather than a shared default,
and `read_messages` is the only bare call site left. `test_only_the_reader_takes_the_default_
budget` keeps that grep as a locator.

The property, stated carefully, is an ORDERING and not an equality:

    guard depth  >=  visible depth

That is what safety needs, and asserting equality would be asserting the mechanism: the guard is
*allowed* to reach further back than a reader can see, and it does in several places now, since
an expired `e-` record still guards its nonce, a MAX_LIMIT tail can run out of records before it
runs out of budget, and the guard reads the whole retained file where a reader stops at
`READ_BUDGET`. Stricter-than-visible costs nothing and hides nothing. Only the reverse is a hole.

So the thing to hold onto is the direction. `a_visible_record_is_always_still_guarded` asserts it
over the whole state machine, and `test_the_guard_scans_at_least_as_deep_as_a_reader_can_see`
measures both depths directly by giving every record its own key, then pins the guard's remaining
boundary as an equality against physical retention. Neither one counts bytes or restates the scan.

Widening the reader's budget past the guard's would still break the ordering, and it is no longer
reachable by tuning the shared default, because the guard does not take it.
`test_narrowing_only_the_reader_no_longer_makes_a_visible_message_replayable` asserts that: the
state it used to construct on purpose, a record a reader can still see whose nonce is no longer
guarded, cannot be built through `_window` any more. That is what this change buys.

The retention ring is now the boundary rather than a red herring. Records survive on disk for
5-10 MiB (COMPACT_KEEP_BYTES, MAX_ROOM_BYTES), and the guard reads all of it, so *disk* contents
are exactly what says what is guarded. A room file no longer holds records that are readable to
the guard but invisible to it.

Three notes on the model:

- **It asks the implementation for the boundary, then holds it to the consequence.** Rather
  than re-deriving what `_last_nonce` should return — which would test a copy of the scan —
  each rule reads the guard value and asserts `append` refuses at or below it and accepts
  above. What is checked is the *agreement* between two functions called one line apart under
  the same lock, which is where a real bug would live.
- **There is no ordering invariant on nonces on disk.** "One key's nonces ascend by seq" is the
  obvious property and it is FALSE — see `surviving_nonces_may_repeat` for the counterexample
  the model produced within three steps. Anything reading `from` + `nonce` off disk as an
  ordering has to be shown this.
- **The window is tuned down, and it takes a patch to do it.** `READ_BUDGET` is bound into
  `reverse_lines`' default argument, so re-binding the module attribute does nothing — see
  `_window`. That is why this file did not exist sooner: the boundary is unreachable in a test
  without writing a megabyte. It now moves the reader alone, so a test that needs the guard's
  boundary moves `MAX_ROOM_BYTES` with `monkeypatch.setattr` and lets compaction do the work.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import didkey  # noqa: E402
import store  # noqa: E402

# Production has READ_BUDGET (1 MiB) < COMPACT_KEEP_BYTES (5 MiB) < MAX_ROOM_BYTES (10 MiB), so
# the byte window binds before the ring and records outlive their own readability. The ratio is
# preserved rather than the values: a model where the ring bound first would be exercising an
# ordering the service does not have, and would hide the whole point of the coupling above.
WINDOW_BYTES = 512  # ~3 records: a key falls out of the window after a few others post
KEEP_BYTES = 2560
RING_BYTES = 5120

# `e-fast` expires records by timestamp. It is here for one asymmetry: `_last_nonce` never
# consults expiry, so a record no reader can see still guards its nonce. That is the safe
# direction, and it is worth pinning as a decision rather than leaving it to be rediscovered.
ROOMS = ("lobby", "e-fast")

# Text is not what this file is about; `tests/conformance/` covers the sweep. All that matters
# here is that every message SURVIVES it, because `clean_text` raises on text that sweeps to
# nothing and that refusal would otherwise be scored as a refused nonce. Hence a visible first
# character and no trailing space to strip.
_BODY = "abcdefghijklmnopqrstuvwxyz0123456789 -_.,"
_VISIBLE = "abcdefghijklmnopqrstuvwxyz0123456789"
SAFE_TEXT = st.builds(
    lambda first, rest: first + rest.rstrip(),
    st.sampled_from(_VISIBLE),
    st.text(alphabet=_BODY, max_size=20),
)


def _b58(raw: bytes) -> str:
    """base58btc. `didkey` only decodes, so the encode side lives with whoever needs it."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = didkey._B58[rem] + out
    return didkey._B58[0] * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def _did(seed_byte: int) -> str:
    public = bytes(SigningKey(bytes([seed_byte]) * 32).verify_key)
    return f"{didkey.PREFIX}z{_b58(didkey.MULTICODEC_ED25519 + public)}"


# Real keys from fixed seeds: `_write_record` calls `didkey.public_key(did)`, so a made-up
# string would be refused for the wrong reason. Two keys, not five — the property is about one
# key's record being pushed out by *other* traffic, which needs a second writer, not a fourth.
DIDS = (_did(1), _did(2))


@contextlib.contextmanager
def _window(size: int):
    """Shrink the tail that `reverse_lines` scans by default, for the length of one test.

    `reverse_lines(f, chunk_size=65536, max_bytes=READ_BUDGET)` binds the budget as a *default
    argument*, evaluated once at import. So `setattr(store, "READ_BUDGET", n)` — how
    `test_store_stateful.py` tunes every other threshold, and how `config.override` reaches the
    rest of the module — has no effect, and both scans keep reading 1 MiB.

    Patching `__defaults__` is the only way in from a test, and it is why the bounded half of
    the replay contract has never had one: reaching the boundary honestly costs ~250 full-size
    records. A one-line change (`max_bytes: int | None = None`, resolved to `READ_BUDGET` in the
    body) would make this helper unnecessary.

    Note that it moves the budget for `read_messages` alone. `_last_nonce` names
    `MAX_ROOM_BYTES` explicitly, so it is immune to this patch, which is why a test that needs
    the guard's own boundary tunes `MAX_ROOM_BYTES` instead and lets compaction drop records.
    Tuning the reader wider than the guard is the failure this file is about, and it is no
    longer reachable from here at all.
    """
    original = store.reverse_lines.__defaults__
    assert original is not None and len(original) == 2, (
        f"reverse_lines' signature changed: defaults are {original!r}. This patches them "
        "positionally, so it has to be re-read rather than trusted."
    )
    store.reverse_lines.__defaults__ = (original[0], size)
    try:
        # A patch that silently failed would make every assertion below vacuous, so prove it
        # took before handing back control — the same reason generate_vectors.py refuses to
        # write vectors it could not check against the server.
        assert store.reverse_lines.__defaults__[1] == size
        yield
    finally:
        store.reverse_lines.__defaults__ = original


def _records(root: Path, room: str) -> list[dict]:
    """Every record on disk, including ones no read path can reach."""
    path = store.room_path(root, room)
    if not path.exists():
        return []
    return [json.loads(raw) for raw in path.read_bytes().splitlines() if raw.strip()]


def _visible(root: Path, room: str) -> list[dict]:
    """What a reader can actually retrieve, at the largest tail the server will serve."""
    return store.read_messages(root, room, limit=store.MAX_LIMIT)["messages"]


def _age_records(path: Path, seconds: int) -> None:
    """Move every record's `ts` back, so `e-` expiry fires without waiting.

    Record timestamps only — file mtimes drive room reaping, which `test_store_stateful.py`
    already models and which would otherwise churn the whole tree on every step.
    """
    lines = []
    for raw in path.read_bytes().splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        stamped = datetime.strptime(rec["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        rec["ts"] = (stamped - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        lines.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    path.write_bytes(b"".join(lines))


class SignedLane(RuleBasedStateMachine):
    """Signed and unsigned writes interleaved, with the replay window moving under them."""

    def __init__(self) -> None:
        super().__init__()
        self.root = Path(tempfile.mkdtemp(prefix="chat-signed-"))
        tuning = {
            "MAX_ROOM_BYTES": RING_BYTES,
            "COMPACT_KEEP_BYTES": KEEP_BYTES,
            # Not 0. `_reap` returns early only while a marker is younger than REAP_EVERY, so 0
            # means "reap on every write" — a full tree walk per append. Reaping is the store
            # model's subject, not this one's.
            "REAP_EVERY": 1 << 30,
            "SNAPSHOT_EVERY": 1 << 30,
        }
        self.saved = {name: getattr(store, name) for name in tuning}
        for name, value in tuning.items():
            setattr(store, name, value)
        self._window = _window(WINDOW_BYTES)
        self._window.__enter__()

        # Every (did, room, nonce) this machine has got accepted. `replay` draws from here, so
        # it re-sends bytes that genuinely worked once rather than a guess.
        self.accepted: list[tuple[str, str, int]] = []
        # (room, did, nonce) seen more than once on disk — the counterexample to the invariant
        # that is not there. Reported by `surviving_nonces_may_repeat`.
        self.repeats: set[tuple[str, str, int]] = set()
        # Per (room, key) high-water mark of the guard. A guard that comes back *lower* means a
        # record left the scanned window and handed a used nonce back, which is the precondition
        # for a replay ever being accepted. Emitted as an event so a run that only ever exercised
        # the easy half of the boundary is visible in `--hypothesis-show-statistics` rather than
        # passing quietly.
        self.high: dict[tuple[str, str], int] = {}

    def teardown(self) -> None:
        self._window.__exit__(None, None, None)
        for name, value in self.saved.items():
            setattr(store, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------------------- helpers

    def _guard(self, room: str, did: str) -> int | None:
        """What the implementation says the replay boundary currently is.

        Read from `_last_nonce` rather than modelled: the assertions are about whether
        `_write_record` honours it, and a model that predicted both would test only itself.
        """
        return store._last_nonce(self.root, room, did)

    def _append_signed(self, room: str, did: str, nonce: int, text: str):
        guard = self._guard(room, did)
        refused = guard is not None and nonce <= guard
        if guard is not None:
            previous = self.high.get((room, did))
            if previous is not None and guard < previous:
                event("guard rolled back — a used nonce was handed back")
            self.high[(room, did)] = max(guard, previous if previous is not None else guard)
        try:
            record = store.append(self.root, room, "", text, did=did, nonce=nonce)
        except store.StoreError as exc:
            # StoreError is the store's one error type, so a refusal has to be attributed
            # before it is scored. Reading "nothing visible was left" as a replay refusal is
            # how a strategy bug gets reported as a security property holding.
            assert "nonce" in str(exc), (
                f"{room}: refused for a reason that is not the nonce — {exc}"
            )
            assert refused, (
                f"{room}: nonce {nonce} refused against guard {guard}, which does not bar it "
                f"— {exc}"
            )
            event("signed write refused — nonce not above the guard")
            return None
        assert not refused, (
            f"{room}: nonce {nonce} accepted although {guard} was already used by this key — "
            "a captured signed URL just worked twice"
        )
        assert record["from"] == did and record["nonce"] == nonce
        event("signed write accepted")
        self.accepted.append((did, room, nonce))
        return record

    # --------------------------------------------------------------------------------- rules

    @rule(
        room=st.sampled_from(ROOMS),
        did=st.sampled_from(DIDS),
        offset=st.sampled_from([-5, -1, 0, 1, 2, 50]),
        text=SAFE_TEXT,
    )
    def signed_say(self, room: str, did: str, offset: int, text: str) -> None:
        """A nonce chosen *relative to the current guard*, so the boundary is hit from both
        sides on purpose. Sampling absolute integers would spend the budget far above the
        guard, where every write trivially succeeds and nothing is under test."""
        guard = self._guard(room, did)
        nonce = max(0, (guard if guard is not None else 0) + offset)
        self._append_signed(room, did, nonce, text)

    @rule(room=st.sampled_from(ROOMS), texts=st.lists(SAFE_TEXT, min_size=1, max_size=3))
    def unsigned_say(self, room: str, texts: list[str]) -> None:
        """Filler, and the mechanism under test: other people's traffic is what pushes a key's
        newest record out of the window and hands its used nonces back."""
        for text in texts:
            store.append(self.root, room, "filler", text)

    @rule(data=st.data())
    def replay(self, data) -> None:
        """Re-send bytes that were accepted before.

        The attack, spelled out: the same (did, room, nonce) a captured URL carries. It must be
        refused while the record is in the scanned window, and it is *allowed* once that record
        is gone — the bounded guarantee, not a bug. Either way it must agree with `_last_nonce`,
        and the invariants below check what a reader can see at the same moment.
        """
        if not self.accepted:
            return
        did, room, nonce = data.draw(st.sampled_from(self.accepted))
        record = self._append_signed(room, did, nonce, "replay")
        # The whole value of this rule is which branch it lands in, and the accepted branch is
        # by far the rarer one — it needs the record to have aged out first. Emitted so a run
        # that stopped reaching it is visible in the statistics rather than silently green.
        event(f"replay {'ACCEPTED (aged out)' if record else 'refused (guarded)'}")

    @rule(seconds=st.sampled_from([61, store.EPHEMERAL_TTL_SECONDS + 61]))
    def advance(self, seconds: int) -> None:
        """Age the world. Records in `e-fast` stop being readable; the point is that they do not
        stop guarding their nonces, because `_last_nonce` never looks at `ts`."""
        for room in ROOMS:
            path = store.room_path(self.root, room)
            if path.exists():
                _age_records(path, seconds)

    # ---------------------------------------------------------------------------- invariants

    @invariant()
    def a_visible_record_is_always_still_guarded(self) -> None:
        """The security property, in the only form that does not restate the algorithm.

        If a reader can retrieve a signed record, replaying it must still be refused — i.e. the
        guard must be at least that record's nonce. The converse is not asserted and does not
        hold: the guard may outlive visibility (an expired `e-` record still guards, a tail of
        MAX_LIMIT records may not reach as far back as the budget does, and the guard reads the
        whole retained file where a reader stops at READ_BUDGET), and stricter-than-visible is
        the safe direction.

        This is the assertion that would fail if a reader ever scanned deeper than the guard —
        see the module docstring, and
        `test_narrowing_only_the_reader_no_longer_makes_a_visible_message_replayable` for the
        state it keeps out.
        """
        for room in ROOMS:
            newest_visible: dict[str, int] = {}
            for rec in _visible(self.root, room):
                if isinstance(rec.get("nonce"), int) and rec.get("from") in DIDS:
                    newest_visible[rec["from"]] = rec["nonce"]
            for did, nonce in newest_visible.items():
                guard = self._guard(room, did)
                assert guard is not None and guard >= nonce, (
                    f"{room}: {didkey.abbreviate(did)} has a READABLE record at nonce {nonce} "
                    f"but the guard is {guard} — that message's signed URL can be replayed "
                    f"while the original is still on the page"
                )

    @invariant()
    def the_guard_is_the_newest_surviving_record_of_that_key(self) -> None:
        """`_last_nonce` returns the newest, not the highest.

        Identical while nonces ascend, so this pins the scan to the file: a scan returning the
        largest value would keep guarding a nonce whose record is long gone, and one returning
        the oldest would hand every used nonce straight back. Also checks that every signed
        record on disk carries an int nonce — `_last_nonce` skips one that does not, which
        would be a silent hole rather than a parse error.
        """
        for room in ROOMS:
            records = _records(self.root, room)
            for rec in records:
                if didkey.is_did(str(rec.get("from", ""))):
                    assert isinstance(rec.get("nonce"), int), (
                        f"{room}: signed record at seq {rec['seq']} has nonce "
                        f"{rec.get('nonce')!r}, which the replay scan would skip"
                    )
            for did in DIDS:
                mine = [r for r in records if r.get("from") == did and "nonce" in r]
                got = self._guard(room, did)
                if not mine:
                    assert got is None, (
                        f"{room}: guard is {got} for a key with no record at all — a nonce is "
                        "being barred by nothing"
                    )
                    continue
                newest = mine[-1]["nonce"]
                # Inside the window the newest record answers; past it the scan stops early and
                # reports None. Both are correct, and which one applies is a byte count this
                # test deliberately does not recompute (see the module docstring).
                assert got in (newest, None), (
                    f"{room}: guard is {got}, but this key's newest record on disk is at nonce "
                    f"{newest} and the only other lawful answer is None"
                )

    @invariant()
    def surviving_nonces_may_repeat(self) -> None:
        """Deliberately not an assertion. Records what the obvious invariant would have claimed.

        "For one key, nonces ascend by seq" is the property this file was written to assert, and
        it is false: the guard only reaches 1 MiB back, the ring keeps 5-10 MiB, so a key that
        goes quiet while others write can be replayed and the room file then holds two records
        with the same `from`, `nonce` and `text` at different seqs. The model found it in three
        steps.

        Nothing is broken — neither record is readable by then, which is the coupling in the
        module docstring. But `from` + `nonce` read off disk is not a sequence, and any consumer
        treating it as one (an archiver, an export, an offline verifier walking a whole file) is
        wrong in a way this note exists to preempt.
        """
        for room in ROOMS:
            seen: set[tuple[str, int]] = set()
            for rec in _records(self.root, room):
                did, nonce = rec.get("from"), rec.get("nonce")
                if not isinstance(did, str) or did not in DIDS or not isinstance(nonce, int):
                    continue
                if (did, nonce) in seen and (room, did, nonce) not in self.repeats:
                    self.repeats.add((room, did, nonce))
                    # Surfaced rather than asserted, so `--hypothesis-show-statistics` shows
                    # whether a run reached the state at all. A model that stopped reaching it
                    # would still pass, and would be worth a lot less.
                    event("a key has two records at one nonce on disk")
                seen.add((did, nonce))


SignedLane.TestCase.settings = settings(
    max_examples=25,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    # CI cannot bisect a suite that finds a different bug every run.
    derandomize=True,
)

TestSignedLane = SignedLane.TestCase


# ------------------------------------------------------------------------ standalone checks
#
# The machine explores; these pin the specific states it is exploring around. A machine that
# went green because it never reached the boundary looks exactly like one that passed, so both
# sides of the boundary are also asserted directly.


def test_the_dids_are_real_keys() -> None:
    """The machine's DIDs go through `didkey.public_key` on every signed write, so a typo would
    fail every rule for a reason with nothing to do with replay."""
    for did in DIDS:
        assert len(didkey.public_key(did)) == 32
    assert len(set(DIDS)) == len(DIDS)


def test_read_budget_is_bound_into_reverse_lines_not_read_from_the_module() -> None:
    """Why `_window` patches `__defaults__`, stated as a test so it cannot rot silently.

    `READ_BUDGET` is the only threshold in `store.py` a test cannot re-bind: it is a default
    argument, captured once at import. Everything else the stateful models tune —
    MAX_ROOM_BYTES, COMPACT_KEEP_BYTES, REAP_EVERY, SNAPSHOT_EVERY — is read at call time and
    answers to `setattr`.

    This asserts the mechanism, not that it must stay. Resolving the default inside
    `reverse_lines` would let `_window` go away, and this test with it.
    """
    original = store.READ_BUDGET
    try:
        # setattr, not `store.READ_BUDGET = …`: the module binds it to a literal, so a direct
        # assignment is a type error. This is also how the machine tunes the other thresholds.
        setattr(store, "READ_BUDGET", 4096)  # noqa: B010
        assert store.reverse_lines.__defaults__ == (65536, original), (
            "rebinding store.READ_BUDGET moved reverse_lines' default, so `_window` patches "
            "something it no longer needs to — delete it and tune the attribute"
        )
    finally:
        setattr(store, "READ_BUDGET", original)  # noqa: B010


def test_the_guard_scans_at_least_as_deep_as_a_reader_can_see(tmp_path, monkeypatch) -> None:
    """The security property as an ORDERING, measured rather than read off the source.

    `guard depth >= visible depth`. That is the whole requirement and it is deliberately not
    equality against the reader: the guard is allowed to outlive visibility (an expired `e-`
    record still guards, a MAX_LIMIT tail may not reach as far back as the budget does) because
    stricter-than-visible is the safe direction. Only the other direction is a hole.

    INTENTIONAL INVERSION of the bounded-window contract. The version this file shipped with
    took its slack from a coincidence: both scans took `reverse_lines`' default budget, so the
    guard reached exactly READ_BUDGET and stopped there. It asserted that some key had fallen
    past. `_last_nonce` now names `max_bytes=MAX_ROOM_BYTES` and scans the whole ring, so the
    guard's boundary is physical retention. The bounded window is not the contract any more. What
    replaces it is asserted below as an equality against the ring, the one place the guard may
    still stop.

    That policy change is also the mechanical reason the old version went red, rather than any
    broken invariant. `_window` patches `reverse_lines.__defaults__`, so it still moves the
    reader while a call site naming its own budget is immune to it. Every key stayed guarded and
    the ordering held with more slack than before. What failed was the anti-vacuity guard, on a
    boundary the guard no longer has.

    Measured by giving every record its own key, which makes the depths separately observable: a
    key is *visible* if `read_messages` returns its record, *retained* if the record is still in
    the room file at all, *guarded* if `_last_nonce` still answers for it. Plain set containment,
    with no byte counting and no restatement of the algorithm.

    What the ordering still catches, now that no read budget can break it: a guard that skips a
    record a reader can see. `_last_nonce` passes over a line its byte prefilter misses or whose
    `nonce` is not an int. Either lands here rather than in the equality.

    This replaces an earlier version that counted `reverse_lines(f)` call sites in `store.py`
    and asserted there were exactly two. That asserted sameness at the call site, which is a
    mechanism and the wrong shape: it went red on a harmless reformat and it stayed green for a
    read path naming a *wider* budget explicitly. Widening one is exactly the change that has
    since been made. On the guard's side it satisfies the ordering rather than breaking it.
    Which side a wider budget lands on is what a call-site count cannot tell. The locator below
    keeps the useful half of that grep.
    """
    room = "lobby"
    # More keys than the bounded window needed, because compaction has to actually fire. The
    # boundary being measured is the ring dropping records, not a scan giving up early.
    keys = [_did(n) for n in range(3, 51)]  # 48 keys, one record each
    assert len(set(keys)) == len(keys)

    # `setattr` reaches MAX_ROOM_BYTES where `_window` cannot: the compactor and the guard both
    # read it at call time, which is the whole difference between the two knobs. Same values the
    # state machine above uses, keeping the same ratio, so the reader's window still binds before
    # the ring. `_write_record` compacts with `keep=limit // 2`, which lands on KEEP_BYTES here
    # exactly as COMPACT_KEEP_BYTES does in production.
    monkeypatch.setattr(store, "MAX_ROOM_BYTES", RING_BYTES)

    with _window(WINDOW_BYTES):
        for key in keys:
            store.append(tmp_path, room, "", "one record", did=key, nonce=1)

        visible = {r["from"] for r in _visible(tmp_path, room) if r.get("from") in keys}
        retained = {r["from"] for r in _records(tmp_path, room) if r.get("from") in keys}
        guarded = {k for k in keys if store._last_nonce(tmp_path, room, k) is not None}

        # Three ways the assertions below pass while proving nothing, closed first. Nothing
        # readable leaves no depth to compare. Nothing dropped leaves "the guard stops where
        # retention does" with no dropped record to stop at. `visible == retained` makes the
        # ordering an identity, because a reader cannot retrieve what is not on disk.
        assert visible, "no record was readable — the window is too small to compare depths"
        assert len(retained) < len(keys), (
            f"all {len(keys)} keys are still on disk, so compaction never ran and the guard's "
            f"boundary was never crossed. Lower RING_BYTES or write more records"
        )
        assert len(visible) < len(retained), (
            f"{len(visible)} of {len(retained)} retained keys are readable, so the reader's "
            f"window does not bind before the ring and the guard has no slack to measure"
        )

        assert visible <= guarded, (
            f"{sorted(didkey.abbreviate(d) for d in visible - guarded)} have READABLE records "
            f"whose nonce is no longer guarded: the reader now scans deeper than the replay "
            f"guard, so those messages' signed URLs can be replayed while still on the page"
        )
        assert guarded == retained, (
            f"the guard no longer stops exactly where retention does. "
            f"{sorted(didkey.abbreviate(d) for d in retained - guarded)} still have a record in "
            f"the room with their nonce handed back, so a captured URL works twice against a "
            f"record the room is still keeping. "
            f"{sorted(didkey.abbreviate(d) for d in guarded - retained)} are guarded with no "
            f"record left to guard, which is replay state outliving its message"
        )


def test_only_the_reader_takes_the_default_budget() -> None:
    """A locator, not the property. The property is the ordering asserted above.

    An intentional inversion of the bounded-window contract. The count went the other way. This
    asserted TWO default-budget call sites back when the guard's depth was the reader's depth by
    coincidence of one shared default. `_last_nonce` names MAX_ROOM_BYTES now, so `read_messages`
    is the only caller left taking the default. The ordering has slack instead of none: the guard
    scans the whole ring, a reader scans READ_BUDGET. Stricter-than-visible was always the safe
    direction. It is a decision in the source now rather than a coincidence that a single line
    could quietly spend.

    Still narrow, still a prompt. The count says where to look when this file goes red. It never
    says the ordering holds. A third unbudgeted read path would inherit the READER's reach by
    accident rather than by decision. That reach is no longer the guard's, so someone should say
    which it meant. A change to `read_messages`' own call site lands here too, because naming any
    budget there takes the count to zero. Adjust the count and move on.

    The second assertion is not a prompt. `max_bytes=MAX_ROOM_BYTES` inside `_last_nonce` is the
    one line this policy is made of: without it the replay window closes again after 1 MiB of
    newer traffic while every record it stopped guarding is still on the page. Either window is a
    defensible policy, which is exactly why swapping them should cost a red test rather than
    riding along with a tidy-up.
    """
    source = Path(store.__file__).read_text(encoding="utf-8")
    bare = [
        n
        for n, line in enumerate(source.splitlines(), 1)
        if "reverse_lines(f)" in line or "reverse_lines(f):" in line
    ]
    assert len(bare) == 1, (
        f"expected exactly one default-budget reverse_lines call (read_messages); found "
        f"{len(bare)} at lines {bare}. A new unbudgeted read path is not necessarily wrong. It "
        f"takes the reader's budget now and not the guard's, so check it against "
        f"test_the_guard_scans_at_least_as_deep_as_a_reader_can_see before changing this number."
    )
    assert "max_bytes=MAX_ROOM_BYTES" in inspect.getsource(store._last_nonce), (
        "the replay guard stopped naming its budget, so it is back on READ_BUDGET and a captured "
        "signed URL works again once 1 MiB of newer traffic buries the record it wrote, while "
        "that record is still readable at /r/<room>. Reverting the window is a policy decision: "
        "make it here, in the tests that state the policy, not on a call site."
    )


def test_a_replay_is_refused_while_the_record_is_in_the_window(tmp_path) -> None:
    """The unbounded half of the contract."""
    did = DIDS[0]
    with _window(WINDOW_BYTES):
        store.append(tmp_path, "lobby", "", "first", did=did, nonce=7)
        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(tmp_path, "lobby", "", "first", did=did, nonce=7)
        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(tmp_path, "lobby", "", "lower", did=did, nonce=6)
        assert store.append(tmp_path, "lobby", "", "up", did=did, nonce=8)["nonce"] == 8


def test_a_replay_is_accepted_once_the_record_leaves_the_ring(tmp_path, monkeypatch) -> None:
    """The bounded half, rebounded. An intentional inversion of the window contract.

    The property most likely to surprise someone reading `nonce` as a permanent counter: it is
    still not one. What moved is where it stops being one. Filler traffic used to end the
    guarantee the moment it pushed the record past the scanned tail, while the record itself was
    still in the room and still on the page. The guard reads the whole ring now, so the only
    thing that hands a used nonce back is the record physically leaving. Both stages are here in
    order: buried past a reader's window and still refused, then dropped by compaction and
    accepted. Both halves are asserted in one place so the second cannot be quoted without the
    first.

    Reaching the new boundary means shrinking the ring, because at the shipped 10 MiB it is
    ~100k records of filler. The window stays shrunk as well: burial past a reader is the state
    the first stage needs. `_window` cannot reach the guard any more, which is what gives that
    stage its meaning (see `_window`: it moves `read_messages` alone now).
    """
    did = DIDS[0]
    # The guard reads MAX_ROOM_BYTES at call time, so unlike READ_BUDGET this bound answers to
    # setattr. Same pair the machine tunes, same values, for the same reason.
    monkeypatch.setattr(store, "MAX_ROOM_BYTES", RING_BYTES)
    monkeypatch.setattr(store, "COMPACT_KEEP_BYTES", KEEP_BYTES)

    def on_disk() -> list[int]:
        """This key's nonces still in the room file, reachable by a reader or not. The boundary
        is physical retention now, so the file is the thing to ask."""
        return [r["nonce"] for r in _records(tmp_path, "lobby") if r.get("from") == did]

    with _window(WINDOW_BYTES):
        store.append(tmp_path, "lobby", "", "guarded", did=did, nonce=7)
        assert store._last_nonce(tmp_path, "lobby", did) == 7

        # Somebody else's traffic, until the record is past the reader's window. Keyed on that
        # rather than on a filler count, because where it lands is what both stages turn on.
        buried = 0
        while any(m.get("from") == did for m in _visible(tmp_path, "lobby")):
            store.append(tmp_path, "lobby", "filler", f"noise {buried}")
            buried += 1
        assert buried > 0, "the record was never readable, so this stage is not about burial"
        assert on_disk() == [7], (
            "the ring dropped the record already, so the two stages have merged and the "
            "refusal below would prove nothing"
        )
        assert store._last_nonce(tmp_path, "lobby", did) == 7, (
            "traffic alone handed a used nonce back while the record is still in the room: the "
            "guard is scanning a window again, not the ring"
        )
        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(tmp_path, "lobby", "", "guarded", did=did, nonce=7)

        # Out of the ring, which is where the guarantee does end. Compaction fires on the append
        # that takes the file past RING_BYTES and keeps the newest half of it, KEEP_BYTES here,
        # so the oldest record is the first to go.
        for i in range(buried, buried + 400):
            store.append(tmp_path, "lobby", "filler", f"noise {i}")
            if not on_disk():
                break
        assert on_disk() == [], "compaction never dropped the record, so nothing below is tested"
        assert store._last_nonce(tmp_path, "lobby", did) is None

        # The safety condition, checked at the moment the guard drops rather than assumed:
        # nothing a reader can retrieve is being replayed. Stronger than it was and now true by
        # construction, since the guard drops only once the record is off the disk rather than
        # off the page.
        assert not any(m.get("from") == did for m in _visible(tmp_path, "lobby")), (
            "the original is still readable, so this replay would be a visible duplicate"
        )
        assert store.append(tmp_path, "lobby", "", "guarded", did=did, nonce=7)["nonce"] == 7


def test_narrowing_only_the_reader_no_longer_makes_a_visible_message_replayable(tmp_path) -> None:
    """The hole a divergent budget used to open, kept as the test of why it is now closed.

    An intentional inversion of the bounded-window contract. This test used to build the
    divergence and assert the damage: guard narrow, reader wide, so a replay landed as a second
    readable record carrying the same signature, nonce and text as an original still on the
    page. `_last_nonce` now names `MAX_ROOM_BYTES` itself, so `_window` cannot reach it and the
    same construction cannot reach that state. What the ordering above used to hold by a
    coincidence of two default arguments is now structural: the guard scans the whole ring,
    which is as far back as a record physically goes, so no reader can be widened past it.

    The construction is kept and the outcome flips. `_window` still moves the default budget.
    Only `read_messages` takes it now, so narrowing it narrows the reader alone. The guard
    answers 7 regardless. The replay is refused. One record stays one record.

    The old behaviour is shown rather than described, because that is the half worth keeping:
    the tail the old guard scanned is scanned here the old way, showing this key's record is not
    in it. That is the byte state in which a captured signed URL used to work a second time.

    Goes red again if the guard ever takes a budget a reader can be widened past.
    """
    did = DIDS[0]
    narrow, wide = 256, 1 << 20
    with _window(wide):
        store.append(tmp_path, "lobby", "", "the guarded message", did=did, nonce=7)
        for i in range(6):
            store.append(tmp_path, "lobby", "filler", f"noise {i}")
        assert any(m.get("from") == did for m in _visible(tmp_path, "lobby")), (
            "the original must be readable for this to demonstrate anything"
        )
        assert store._last_nonce(tmp_path, "lobby", did) == 7  # guarded by the ring now

    # Exactly one change: the reader's reach. It cannot be the guard's any more, because
    # `_window` moves the default and `_last_nonce` names its own budget.
    with _window(narrow):
        # The tail the old guard read, read the old way. Nothing of this key's is in it, so an
        # unbudgeted scan answered None here and handed nonce 7 straight back. Checked as bytes
        # rather than assumed: a `narrow` that still reached the record would make the refusal
        # below pass for a reason that has nothing to do with the change.
        with store.room_path(tmp_path, "lobby").open("rb") as f:
            in_default_tail = [raw for raw in store.reverse_lines(f) if did.encode() in raw]
        assert not in_default_tail, (
            f"{narrow} bytes still reaches this key's record, so the old default guarded it too "
            f"and the refusal below shows nothing. Write more filler."
        )

        assert store._last_nonce(tmp_path, "lobby", did) == 7, (
            "the guard took the patched default, so its reach is whatever a reader's is and "
            "the ordering this file protects is back to a coincidence"
        )
        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(tmp_path, "lobby", "", "the guarded message", did=did, nonce=7)

    with _window(wide):
        mine = [m for m in _visible(tmp_path, "lobby") if m.get("from") == did]
        assert len(mine) == 1, f"expected the original alone, got {len(mine)}"
        assert mine[0]["nonce"] == 7
        assert mine[0]["text"] == "the guarded message"
        # One signature over `lobby|7|the guarded message`, still authenticating one record.
        # Attribution and distinctness both intact. Distinctness no longer rests on two
        # budgets happening to match.
    # Refused before `seq` is drawn, so the file holds no second copy either. That is this
    # construction, not a general rule: `surviving_nonces_may_repeat` has the case where the
    # ring drops the original first and a repeat on disk is lawful.
    assert len([r for r in _records(tmp_path, "lobby") if r.get("from") == did]) == 1


def test_an_expired_record_still_guards_its_nonce(tmp_path) -> None:
    """`_last_nonce` scans the raw file and never parses `ts`, so a record no reader can see
    still refuses its own replay.

    The safe direction, and the asymmetry worth stating: in an `e-` room the replay window is
    bounded by BYTES, not by the TTL. A reader sees nothing; a replayer is still refused.
    """
    did = DIDS[0]
    room = "e-fast"
    with _window(WINDOW_BYTES):
        store.append(tmp_path, room, "", "ephemeral", did=did, nonce=3)
        _age_records(store.room_path(tmp_path, room), store.EPHEMERAL_TTL_SECONDS + 61)
        assert _visible(tmp_path, room) == [], (
            "the record is still readable, so this says nothing about expired records"
        )
        assert store._last_nonce(tmp_path, room, did) == 3
        with pytest.raises(store.StoreError, match="not greater than 3"):
            store.append(tmp_path, room, "", "ephemeral", did=did, nonce=3)
