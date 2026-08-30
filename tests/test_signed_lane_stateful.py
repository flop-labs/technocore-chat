"""A Hypothesis state machine over the signed lane's replay defence.

Run: uv run --group dev python -m pytest tests/test_signed_lane_stateful.py

`tests/test_store_stateful.py` models the store lifecycle. This file models the signed
lane property whose correctness is security-sensitive: `_last_nonce`, which decides
whether a previously accepted nonce may be used again.

The contract is bounded by physical room retention. Ordinary `read_messages` remains
bounded by READ_BUDGET, while `_last_nonce` may scan deeper: a signed nonce remains spent
while that nonce, or a later nonce from the same key that would reject it, survives in
the retained room file. Once retention forgets all such history, the old nonce may be
accepted again.

The safety ordering is therefore:

    guard depth >= visible depth

Equality is neither required nor desired. A record may leave the ordinary read window,
or become invisible through an ephemeral TTL, while still participating in replay
protection. Physical retention is the replay authority.

The state machine deliberately asks `_last_nonce` for the current guard rather than
reimplementing the scan. It then checks that the write path agrees with that guard,
that every visible signed record remains guarded, and that the guard equals the newest
physically retained canonical record for the key.

READ_BUDGET is patched down in tests so the ordinary visibility boundary is reachable
without writing a megabyte. That patch changes the default used by ordinary readers;
the retained-history replay scan deliberately supplies its own explicit byte bound and
therefore does not shrink with the reader.
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

# Production has READ_BUDGET (1 MiB) < COMPACT_KEEP_BYTES (5 MiB) < MAX_ROOM_BYTES (10 MiB).
# Preserve that ordering here so a record can leave ordinary visibility while remaining physically
# retained and therefore still guarded. This is the deliberate slack in guard depth >= visible depth.
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
    """Shrink `reverse_lines`' default budget, which controls ordinary tail reads.

    `reverse_lines(f, chunk_size=65536, max_bytes=READ_BUDGET)` binds READ_BUDGET as a
    default argument at import time, so rebinding `store.READ_BUDGET` would not move it.

    `_last_nonce` no longer inherits this default: it explicitly supplies the size of the
    open retained room file. This helper therefore moves ordinary visibility without
    narrowing replay authority, which is exactly the policy boundary these tests exercise.
    """
    original = store.reverse_lines.__defaults__
    assert original is not None and len(original) == 2, (
        f"reverse_lines' signature changed: defaults are {original!r}. This patches them "
        "positionally, so it has to be re-read rather than trusted."
    )
    store.reverse_lines.__defaults__ = (original[0], size)
    try:
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
    """Signed and unsigned writes interleaved while visibility and retention move."""

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
        # that is not there. The retained-history invariants below catch that state.
        # Per (room, key) observed guard high-water mark. A later rollback can occur only
        # when physical retention has forgotten newer nonce history; surface that transition
        # in Hypothesis statistics because it is where an older nonce may become spendable.
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
                event("guard rolled back — retention forgot newer nonce history")
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
        """Try a nonce that was accepted before.

        It is refused while retained nonce history still guards it. It may be accepted only
        after physical retention has forgotten that nonce and every later nonce from the same
        key that would still reject it.
        """
        if not self.accepted:
            return
        did, room, nonce = data.draw(st.sampled_from(self.accepted))
        record = self._append_signed(room, did, nonce, "replay")
        event(
            "replay ACCEPTED (retention forgot guard)"
            if record
            else "replay refused (retained history guards it)"
        )

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
        """Every visible signed record is protected by an equal-or-deeper replay guard.

        The converse is deliberately false: the replay guard may outlive ordinary visibility
        because it follows physical retention. This directly asserts the safe ordering
        `guard depth >= visible depth`.
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
        """The guard equals the newest physically retained canonical record for that key.

        `_last_nonce` scans newest-first. In this model every signed record is written by the
        store itself, so any surviving record is authoritative replay history and the newest
        one must answer the lookup exactly.
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
                    assert got is None, f"{room}: guard is {got} for a key with no retained record"
                    continue

                newest = mine[-1]["nonce"]
                assert got == newest, (
                    f"{room}: guard is {got}, but this key's newest physically retained "
                    f"record is nonce {newest}"
                )

    @invariant()
    def retained_nonces_are_strictly_increasing_per_key(self) -> None:
        """Retained history written through this store has increasing nonces per key.

        For records accepted through the store, a lower nonce may become usable again only
        after the retained history that barred it is physically gone. Therefore two surviving
        store-written records for one key cannot carry the same nonce, and their nonces must
        increase with sequence order. A foreign or hand-written record whose DID is JSON-escaped
        can be parsed here while remaining outside _last_nonce's raw-byte prefilter; that
        deliberately separate boundary is covered in test_store.py.
        """
        for room in ROOMS:
            records = _records(self.root, room)
            for did in DIDS:
                nonces = [
                    rec["nonce"]
                    for rec in records
                    if rec.get("from") == did and isinstance(rec.get("nonce"), int)
                ]
                assert all(a < b for a, b in zip(nonces, nonces[1:], strict=False)), (
                    f"{room}: retained nonce history for {didkey.abbreviate(did)} is not "
                    f"strictly increasing: {nonces}"
                )


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
    """The security property as an ORDERING, measured rather than inferred from source.

    `guard depth >= visible depth` remains the invariant. The replay guard is deliberately
    allowed to extend beyond the ordinary read window: #466 makes physical room retention,
    rather than READ_BUDGET, the guard boundary.

    Every record gets its own key so the two depths are independently observable. This
    fixture must cross the ordinary read boundary while leaving the records physically
    retained; the result should therefore demonstrate the safe strict ordering directly:
    some keys are no longer visible, while every retained key is still guarded.
    """
    room = "lobby"
    keys = [_did(n) for n in range(3, 19)]
    assert len(set(keys)) == len(keys)

    with _window(WINDOW_BYTES):
        for key in keys:
            store.append(tmp_path, room, "", "one record", did=key, nonce=1)

        visible = {r["from"] for r in _visible(tmp_path, room) if r.get("from") in keys}
        guarded = {k for k in keys if store._last_nonce(tmp_path, room, k) is not None}

        assert visible, "no record was readable — the window is too small to compare depths"
        assert len(visible) < len(keys), (
            "all records are still readable, so this fixture did not cross the ordinary "
            "READ_BUDGET boundary"
        )
        assert guarded == set(keys), (
            "a physically retained signed record lost replay authority merely because it "
            "left the ordinary read window"
        )
        assert visible < guarded


def test_only_the_room_reader_takes_the_default_budget() -> None:
    """A structural locator for the intentional split between read and replay budgets.

    Ordinary `read_messages` remains bounded by `reverse_lines`' default READ_BUDGET.
    `_last_nonce` deliberately supplies the size of the open retained room file instead,
    so replay authority may extend beyond what the ordinary tail reader can see.

    This is only a locator; the behavioral ordering is asserted by
    `test_the_guard_scans_at_least_as_deep_as_a_reader_can_see`.
    """
    source = Path(store.__file__).read_text(encoding="utf-8")
    bare = [
        n
        for n, line in enumerate(source.splitlines(), 1)
        if "reverse_lines(f)" in line or "reverse_lines(f):" in line
    ]

    assert len(bare) == 1, (
        f"expected exactly one default-budget reverse_lines call (read_messages); "
        f"found {len(bare)} at lines {bare}"
    )
    assert "reverse_lines(f, max_bytes=os.fstat(f.fileno()).st_size)" in source, (
        "_last_nonce must scan the physically retained room file rather than inherit "
        "the ordinary READ_BUDGET"
    )


def test_nonce_lookup_uses_retained_file_size_and_stops_at_newest_match(
    tmp_path, monkeypatch
) -> None:
    """A recent signer pays only to its newest record even though the allowed bound is deeper."""
    room = "lobby"
    did = DIDS[0]
    path = store.room_path(tmp_path, room)
    path.parent.mkdir(parents=True, exist_ok=True)

    newest = json.dumps(
        {
            "seq": 2,
            "ts": store._now(),
            "from": did,
            "text": "newest",
            "nonce": 8,
        }
    ).encode()

    path.write_bytes(b'{"seq":1,"from":"~filler","text":"old"}\n' + newest + b"\n")
    physical_size = path.stat().st_size
    seen: dict[str, int] = {}

    def counted(f, chunk_size=65536, max_bytes=store.READ_BUDGET):
        seen["max_bytes"] = max_bytes
        yield newest
        raise AssertionError("_last_nonce scanned past the newest matching record")

    monkeypatch.setattr(store, "reverse_lines", counted)

    assert store._last_nonce(tmp_path, room, did) == 8
    assert seen["max_bytes"] == physical_size


def test_nonce_lookup_covers_physically_retained_bytes_beyond_nominal_room_cap(
    tmp_path, monkeypatch
) -> None:
    """Crash-before-compaction bytes remain replay authority while physically retained."""
    room = "lobby"
    did = DIDS[0]
    path = store.room_path(tmp_path, room)
    path.parent.mkdir(parents=True, exist_ok=True)

    signed = (
        json.dumps(
            {
                "seq": 1,
                "ts": store._now(),
                "from": did,
                "text": "guarded",
                "nonce": 7,
            }
        ).encode()
        + b"\n"
    )
    filler = b'{"seq":2,"from":"~filler","text":"xxxxxxxxxxxxxxxxxxxxxxxx"}\n' * 8

    path.write_bytes(signed + filler)
    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 128)

    raw = path.read_bytes()
    assert len(raw) > store.MAX_ROOM_BYTES
    assert did.encode() not in raw[-store.MAX_ROOM_BYTES :]
    assert store._last_nonce(tmp_path, room, did) == 7


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


def test_a_replay_is_refused_after_the_record_leaves_the_read_window_while_retained(
    tmp_path,
) -> None:
    """Leaving READ_BUDGET no longer ends replay authority.

    Another writer may bury the signed record beyond the ordinary readable tail while the
    room file still physically retains it. The old policy accepted the captured write at
    that point; the retention-aligned policy continues rejecting it until retention actually
    forgets the relevant nonce history.
    """
    did = DIDS[0]
    room = "lobby"

    with _window(WINDOW_BYTES):
        store.append(tmp_path, room, "", "guarded", did=did, nonce=7)
        path = store.room_path(tmp_path, room)

        assert store._last_nonce(tmp_path, room, did) == 7

        for i in range(40):
            store.append(tmp_path, room, "filler", f"noise {i}")

        assert not any(r.get("from") == did for r in _visible(tmp_path, room)), (
            "the original is still in the ordinary read window, so this fixture has not "
            "crossed the policy boundary it claims to test"
        )
        assert did.encode() in path.read_bytes(), (
            "the original signed record was physically removed, so this is testing "
            "retention loss rather than the difference between read and replay depth"
        )

        assert store._last_nonce(tmp_path, room, did) == 7

        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(tmp_path, room, "", "guarded", did=did, nonce=7)

        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(tmp_path, room, "", "older", did=did, nonce=6)

        assert store.append(tmp_path, room, "", "newer", did=did, nonce=8)["nonce"] == 8


def test_narrowing_the_read_budget_does_not_narrow_the_replay_guard(tmp_path) -> None:
    """Read visibility and replay authority deliberately have different bounds.

    #466 decouples the replay guard from the ordinary READ_BUDGET. Narrowing the
    reader's tail may make an older signed record invisible to an ordinary read,
    but it must not make the captured signed write reusable while that record is
    still physically retained.

    This is the intentional replacement for the old coupling test: guard depth
    may be strictly greater than visible depth, but never smaller.
    """
    did = DIDS[0]
    narrow, wide = 256, 1 << 20
    room = "lobby"

    with _window(wide):
        store.append(tmp_path, room, "", "the guarded message", did=did, nonce=7)
        for i in range(6):
            store.append(tmp_path, room, "filler", f"noise {i}")

        assert any(m.get("from") == did for m in _visible(tmp_path, room)), (
            "the original must initially be readable for this comparison"
        )
        assert store._last_nonce(tmp_path, room, did) == 7

    path = store.room_path(tmp_path, room)
    assert did.encode() in path.read_bytes(), (
        "the signed record must remain physically retained for the replay guard to have authority"
    )

    with _window(narrow):
        assert store._last_nonce(tmp_path, room, did) == 7

        with pytest.raises(store.StoreError, match="not greater than 7"):
            store.append(
                tmp_path,
                room,
                "",
                "the guarded message",
                did=did,
                nonce=7,
            )


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
