"""A Hypothesis state machine over the signed lane's replay defence.

Run: uv run --group dev python -m pytest tests/test_signed_lane_stateful.py

`tests/test_store_stateful.py` models the store's lifecycle — append, read, expire, compact,
reap, note CAS — and never touches a signature. This models the part of the store whose
correctness is a *security* property: `_last_nonce`, which decides whether a captured signed
URL still works.

The contract is deliberately bounded. A signed URL is a bearer token for exactly one message,
so replaying it must fail while the message is still there to be seen; once the record has aged
out the replay is accepted again as a fresh message, and `_last_nonce`'s docstring says so. The
bound is the retention model doing what it says rather than a hole.

WHY THE BOUND IS SAFE, which is not where it looks
--------------------------------------------------
"Refused while the original is readable" is not established by anything in `_write_record`. It
holds because of a coincidence of two default arguments:

    read_messages    for raw in reverse_lines(f):        <- default max_bytes
    _last_nonce      for raw in reverse_lines(f):        <- default max_bytes

Those are the *only* two call sites in the module that take `reverse_lines`' default budget.
Every other one names its own and narrower (`last_seq` 64 KiB, the tripwire window 64 KiB) or is
a write path (`_compact`, MAX_ROOM_BYTES).

The property that follows, stated carefully, is an ORDERING and not an equality:

    guard depth  >=  visible depth

Today the two are the same 1 MiB, which satisfies it with no slack. But equality is not what
safety needs, and asserting equality would be asserting the mechanism: the guard is *allowed* to
reach further back than a reader can see, and in two places it already does — an expired `e-`
record still guards its nonce, and a MAX_LIMIT tail can run out of records before it runs out of
budget. Stricter-than-visible costs nothing and hides nothing. Only the reverse is a hole.

So the thing to hold onto is the direction, not the coincidence. `a_visible_record_is_always_
still_guarded` asserts it over the whole state machine, and
`test_the_guard_scans_at_least_as_deep_as_a_reader_can_see` measures both depths directly by
giving every record its own key. Neither one counts bytes or restates the scan.

Widen the reader's budget and not the guard's and the ordering is gone.
`test_narrowing_only_the_guards_budget…` below constructs that state on purpose: a record a
reader can still see, whose nonce is no longer guarded, so the replay lands as a second visible
record with the same signature, nonce and text as the first. That is what a "let readers page
further back" change costs if it touches `read_messages`' budget alone, and it is the state
these tests exist to keep unreachable. Nothing in the repo records that this is load-bearing,
which is the gap this file closes.

The retention ring is a red herring here, and worth naming because it looks relevant: records
survive on disk for 5-10 MiB (COMPACT_KEEP_BYTES, MAX_ROOM_BYTES), far past the 1 MiB either
window reaches. So a room file legitimately holds records no reader can ever retrieve, and
*disk* contents say nothing about what is guarded.

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
  without writing a megabyte.
"""

from __future__ import annotations

import contextlib
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

    Note that it moves the budget for `read_messages` and `_last_nonce` *together*, which is
    exactly the coupling the module docstring describes. Tuning them apart is the failure this
    file is about, and only one test does it, deliberately.
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
        hold: the guard may outlive visibility (an expired `e-` record still guards, and a tail
        of MAX_LIMIT records may not reach as far back as 1 MiB does), and stricter-than-visible
        is the safe direction.

        This is the assertion that would fail if `read_messages`' and `_last_nonce`' scan
        budgets ever diverged — see the module docstring, and
        `test_narrowing_only_the_guards_budget_makes_a_visible_message_replayable` for the
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


def test_the_guard_scans_at_least_as_deep_as_a_reader_can_see(tmp_path) -> None:
    """The security property as an ORDERING, measured rather than read off the source.

    `guard depth >= visible depth`. That is the whole requirement, and it is deliberately not
    equality: the guard is allowed to outlive visibility — an expired `e-` record still guards,
    and a MAX_LIMIT tail may not reach as far back as the budget does — because
    stricter-than-visible is the safe direction. Only the other direction is a hole.

    Measured by giving every record its own key, which makes the two depths separately
    observable: a key is *visible* if `read_messages` returns its record, and *guarded* if
    `_last_nonce` still answers for it. The property is then plain set containment, with no
    byte counting and no restatement of the algorithm.

    This replaces an earlier version that counted `reverse_lines(f)` call sites in `store.py`
    and asserted there were exactly two. That asserted sameness at the call site, which is a
    mechanism and the wrong shape: it went red on a harmless reformat, and it stayed green for
    a new read path that named a *wider* budget explicitly — the one change that actually
    breaks the ordering. `test_only_two_read_paths_take_the_default_budget` keeps the useful
    half of that grep as a locator, below.
    """
    room = "lobby"
    keys = [_did(n) for n in range(3, 19)]  # 16 keys, one record each
    assert len(set(keys)) == len(keys)

    with _window(WINDOW_BYTES):
        for key in keys:
            store.append(tmp_path, room, "", "one record", did=key, nonce=1)

        visible = {r["from"] for r in _visible(tmp_path, room) if r.get("from") in keys}
        guarded = {k for k in keys if store._last_nonce(tmp_path, room, k) is not None}

        # Both directions have to be non-trivial or the containment below proves nothing: if
        # everything is guarded the assertion is vacuous, and if nothing is visible there is no
        # depth to compare against.
        assert visible, "no record was readable — the window is too small to compare depths"
        assert len(guarded) < len(keys), (
            f"all {len(keys)} keys are still guarded, so the boundary was never crossed and "
            f"this test is vacuous — lower WINDOW_BYTES or write more records"
        )

        assert visible <= guarded, (
            f"{sorted(didkey.abbreviate(d) for d in visible - guarded)} have READABLE records "
            f"whose nonce is no longer guarded: the reader now scans deeper than the replay "
            f"guard, so those messages' signed URLs can be replayed while still on the page"
        )


def test_only_two_read_paths_take_the_default_budget() -> None:
    """A locator, not the property — the property is the ordering asserted above.

    `read_messages` and `_last_nonce` are the only `reverse_lines` call sites in the module that
    pass no `max_bytes`. That is worth knowing when this file goes red, because it says where to
    look; it is not itself the guarantee, and a green result here does not mean the ordering
    holds.

    Kept deliberately narrow: if a third unbudgeted read path appears, it inherits the guard's
    reach by accident rather than by decision, and someone should say which it meant. Adjust the
    count and move on — this is a prompt, not a veto.
    """
    source = Path(store.__file__).read_text(encoding="utf-8")
    bare = [
        n
        for n, line in enumerate(source.splitlines(), 1)
        if "reverse_lines(f)" in line or "reverse_lines(f):" in line
    ]
    assert len(bare) == 2, (
        f"expected exactly two default-budget reverse_lines calls (read_messages and "
        f"_last_nonce); found {len(bare)} at lines {bare}. A new unbudgeted read path is not "
        f"necessarily wrong — but check it against "
        f"test_the_guard_scans_at_least_as_deep_as_a_reader_can_see before changing this number."
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


def test_a_replay_is_accepted_once_the_record_leaves_the_window(tmp_path) -> None:
    """The bounded half — documented on `_last_nonce` and, until now, untested.

    The property most likely to surprise someone reading `nonce` as a permanent counter: it is
    not one. The guarantee is "not twice while the message can be read", and filler traffic
    from another writer is enough to end it. Both halves are asserted in one place so the
    second cannot be quoted without the first.
    """
    did = DIDS[0]
    with _window(WINDOW_BYTES):
        store.append(tmp_path, "lobby", "", "guarded", did=did, nonce=7)
        assert store._last_nonce(tmp_path, "lobby", did) == 7
        for i in range(40):  # somebody else's traffic, pushing it past WINDOW_BYTES
            store.append(tmp_path, "lobby", "filler", f"noise {i}")
        assert store._last_nonce(tmp_path, "lobby", did) is None, (
            "the record is still inside the scanned window, so this is not testing the "
            "boundary it claims to — raise the filler count"
        )
        # The safety condition, checked at the moment the guard drops rather than assumed:
        # nothing a reader can retrieve is being replayed.
        assert not any(m.get("from") == did for m in _visible(tmp_path, "lobby")), (
            "the original is still readable, so this replay would be a visible duplicate"
        )
        assert store.append(tmp_path, "lobby", "", "guarded", did=did, nonce=7)["nonce"] == 7


def test_narrowing_only_the_guards_budget_makes_a_visible_message_replayable(tmp_path) -> None:
    """What the shared default is buying, shown by taking it away.

    Constructs the divergence on purpose: the guard scans a narrow tail while a reader scans a
    wide one. Nothing in the repo does this — the point is that one line could, and that the
    result is not a subtle degradation. The replay lands as a second readable record with the
    same signature, nonce and text as an original the reader can still see, in the same room,
    at a later seq and ts.

    Delete this test if `reverse_lines` ever grows separate budgets on purpose, and replace it
    with whatever then keeps the two in order.
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
        assert store._last_nonce(tmp_path, "lobby", did) == 7  # coupled: still guarded

    # Exactly one change: the guard's reach, not the reader's.
    with _window(narrow):
        assert store._last_nonce(tmp_path, "lobby", did) is None
        replayed = store.append(tmp_path, "lobby", "", "the guarded message", did=did, nonce=7)

    with _window(wide):
        mine = [m for m in _visible(tmp_path, "lobby") if m.get("from") == did]
        assert len(mine) == 2, f"expected the original and the replay, got {len(mine)}"
        first, second = mine
        assert first["nonce"] == second["nonce"] == 7
        assert first["text"] == second["text"] == "the guarded message"
        assert first["seq"] < second["seq"] == replayed["seq"]
        # One signature over `lobby|7|the guarded message` now authenticates both records, and
        # each verifies offline. Attribution is intact; distinctness is not.


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
