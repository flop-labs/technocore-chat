"""Crash-safe nonce allocation for the signed lane.

The server's rule is that a nonce must be strictly greater than the last one *that key*
used *in that room* — per (key, room), never global. A client that allocates badly does
not fail loudly: it gets a 4xx naming a number it thought it had never used, and the
cause is a file it wrote three restarts ago.

Two properties, and the second is the whole reason this file exists rather than a counter
in memory:

**Monotonic across concurrent processes, not merely across restarts.** The first version of
this claimed the former and delivered the latter: the wall clock plus a process-local
high-water mark keeps a *restart* safe, and does nothing for two processes that load the same
persisted value in the same millisecond — they compute the same number and one signed write is
refused. Raised by @yukkie3276 in review, and correct. Reload, allocate and flush now happen
under an exclusive `flock` on a sidecar lock file, so the read-modify-write is serialized
across processes and each one sees the previous one's number.

Losing the file still degrades to "probably fine" rather than "reuses from 1", because the
clock remains a floor.

**Persisted before it is returned, never after.** The caller may crash, or the network may
swallow the write, between being handed a nonce and the server seeing it. If the record
were written after a successful send, that gap would hand the same number out twice. It is
written first and fsynced, so a nonce that was *never used* is simply skipped — and skipped
nonces cost nothing, while a repeat is a rejected write the caller cannot explain.

The file is rewritten whole under `os.replace`, which is atomic on POSIX and on Windows for
the same path: a reader either sees the old file or the new one, never a truncated one. The
temporary it is renamed from carries the pid, because a fixed `.tmp` name is a second way two
processes collide — one writing it while the other renames it — which is the same defect as
the nonce race wearing different clothes.

The containing directory is fsynced too, because on ext4 the rename can otherwise outlive the
data it points at across a power cut.

**POSIX.** `fcntl.flock` and fsyncing a directory handle are both POSIX; this module does not
run on Windows and says so here rather than failing at import with a message about a missing
attribute. That was already true of the directory fsync before the lock was added.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .durable import fsync_dir, mkdir_durable

# The highest nonce this *process* has issued, across every store object and every pair.
# A store rebuilt from a deleted file knows nothing, and the clock alone cannot separate
# two allocations landing in the same millisecond — so the one thing still available, the
# fact that this process already handed out that number, is kept here. It bounds the
# realistic loss case (a file removed while the program runs, or a second store opened on
# the same path) to something strictly increasing.
#
# What it does not cover, and no in-process value could: file lost AND a new process AND
# the same millisecond as the last write. That needs the persisted state, which is the
# point of the file; the residue is one refused write and a retry that succeeds a
# millisecond later.
_process_floor = 0

# The ledger's "initialised and never used" value. It used to be exactly `{}`, and `{}` is also
# what you get by truncating a populated ledger — so the innocent state and an erased one were
# byte-identical and no check could separate them (@Minh3132, #803). A file this class wrote
# carries this key; a file wiped to `{}` does not, and beside an existing key that is unknown.
#
# Not a DID, and cannot be mistaken for one: every DID begins `did:`, so `!` can never collide
# with a real entry in the same object.
_MARKER = "!ledger"
_FORMAT_VERSION = 1


class LedgerUnreadableError(ValueError):
    """A ledger that exists and cannot be trusted, carrying its reason apart from its advice.

    Two callers need different halves of the same refusal. An operator wants the whole message,
    ending in how to recover. `Signer` wants only the reason, because its own advice is the
    opposite one — for a *lost key*, moving the ledger aside is the catastrophic step — and it
    has to quote why the file is unreadable inside a different recommendation. It used to get
    that by splitting the formatted string on the first ". ", which a `repr` containing ". "
    would clip. The reason is data; formatting it and parsing it back is not.
    """

    def __init__(self, why: str, message: str) -> None:
        super().__init__(message)
        self.why = why


class _DuplicateLedgerKeyError(Exception):
    """A key repeated inside one JSON object, carrying its name so the refusal can say it.

    Deliberately not a `ValueError`: `_load` turns one of those into "could not be read as JSON",
    and this is the opposite fact. The file parsed perfectly; the parser is what lost the data.
    Folding it into the parse-failure message would send an operator looking for damaged syntax
    in a file whose syntax is fine.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """`object_pairs_hook` that refuses a repeated key rather than keeping the last one.

    `json.loads` accepts `{"room": 900, "room": 5}` and returns `{"room": 5}` without a word. A
    ledger damaged that way satisfies every check below — valid JSON, an object, an entry of the
    right shape — while the higher number is gone before any of them runs. `_allocate_locked`
    then treats 5 as the last nonce issued, and after a clock rollback allocates beneath the
    server's retained replay floor, which is the failure the whole loader exists to prevent
    (@Minh3132, #803). A repeated DID discards an entire room map by the same mechanism.

    This is the defect `keyring.py` fixes with `base64.b64decode(..., validate=True)`, in a
    different parser: a permissive default silently changing the meaning of damaged input, where
    the leniency *is* the bug. The package refuses both now rather than interpreting either.

    Every key is treated alike, `_MARKER` included. `_load` pops the marker after parsing, so it
    is an ordinary key at this point, and a file carrying two of them is exactly as unreadable as
    one carrying two DIDs.
    """
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateLedgerKeyError(key)
        seen[key] = value
    return seen


class NonceStore:
    """Per-(did, room) allocation, persisted before each number is handed out."""

    def __init__(self, path: str | Path, *, key_exists: bool | Callable[[], bool] = False) -> None:
        """`key_exists` says whether a signing key exists at this home — a bool, or a callable
        asked afresh at every load.

        Pass the callable whenever the answer can change while this store is alive. It can:
        `Signer` has to build the store before it mints the key, so a boolean captured there is
        false for the whole life of the store that created the identity (@yukkie3276, #803). A
        snapshot of a fact that is about to change is not a cheaper version of the fact.

        Absence is only proof of a first run when nothing has run before. Once a key exists, an
        absent ledger is not "nothing was issued" — it is "something may have been issued and
        the record is gone", which is the same unknown as a corrupt one and gets the same
        refusal (@yukkie3276, #803).

        The judgement still cannot be made here, and the default says so by being `False`. A store
        on its own sees a path and nothing else; only something that knows whether a *key* exists
        can tell a first run from a loss, and that is `Signer`, which creates an empty ledger
        before the key so that seed-without-ledger can only mean the record went missing. Handing
        over a question rather than an answer keeps that division and drops the staleness.
        Constructed directly, a `NonceStore` therefore still degrades to the clock floor on a
        lost file — weaker, and asserted rather than assumed, because deleting the test that
        covered it would have left the weaker path unexamined.
        """
        self._path = Path(path)
        # A question, not an answer. Passed as a plain `bool` this was read once at
        # construction, and `Signer` constructs the store BEFORE `Keyring` mints the key —
        # so on a first run the store held "no key here" for its whole life, and a ledger
        # deleted later in that same process was read as a first run by the very install
        # that had just created the identity (@yukkie3276, #803). A callable is asked at
        # every load, so there is no snapshot left to go stale.
        self._key_exists: Callable[[], bool] = (
            key_exists if callable(key_exists) else (lambda: bool(key_exists))
        )
        self._marker: dict[str, int] = {"v": _FORMAT_VERSION}
        self._marker_present = False
        self._state: dict[str, dict[str, int]] = self._load()

    def initialise(self) -> None:
        """Write an empty ledger if there is none, so its later absence is evidence.

        Under the same lock every other write takes, and re-checking inside it. The first
        version checked `exists()` and then flushed with no lock at all, which put a
        first-start clobber *into the fix for a first-start race* (@yukkie3276, #803): two
        starters both see the ledger absent, A initialises, mints its key, allocates and
        persists nonce N — and B, still holding the stale answer to a question it asked before
        any of that, replaces `{}` over A's populated ledger. Nothing was deleted and nothing
        was corrupted, and a durable nonce is gone anyway, which lands the next restart back in
        exactly the unknown-floor case this whole change exists to close.

        `_flush` publishes with `os.replace`, so it overwrites by design; the only thing that
        can stop it is not calling it, and the only way to know is to look while holding the
        lock.
        """
        mkdir_durable(self._path.parent)
        fd = self._lock()
        try:
            if self._path.exists():
                return
            self._state = {}
            self._marker = {"v": _FORMAT_VERSION}
            self._flush()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def records(self) -> tuple[int, int, bool]:
        """(keys recorded, (key, room) pairs recorded), from one read.

        Exists so `Signer` can tell an empty ledger from one with history without reaching into
        private state — and it returns both numbers because the first version returned only the
        pair count, which is not the same predicate. `{"did:key:z...": {}}` holds zero pairs and
        is not empty: `allocate` never writes a key with no rooms, so a key sitting there means
        something happened that this class did not do. The innocent value is a ledger holding the
        initialisation marker and no entries, which is what `initialise()` writes, and anything
        else is history (second reader, #803). The marker is stripped by `_load`, so it is never
        counted here as a key.
        """
        # Through `_reload` like the other two readers, rather than a bare `_load`. Its single
        # caller runs before anything has allocated, so the lock changes no answer today — but
        # three read paths with two different locking rules is the kind of asymmetry this file
        # otherwise has to justify in a comment, and the justification would have been "nobody
        # calls it at a bad time yet".
        state = self._reload()
        return len(state), sum(len(rooms) for rooms in state.values()), self._marker_present

    def _load(self) -> dict[str, dict[str, int]]:
        """The persisted ledger, or `{}` when there has never been one.

        **Absent and corrupt are not the same fact**, and the first version of this treated them
        alike. A missing file is a first run: nothing was ever issued, so starting from the clock
        is correct. A file that exists and does not parse is *unknown*: numbers were issued and
        we cannot say which. Starting from the clock then is a guess, and @Minh3132's sequence on
        #803 is when the guess is wrong — a host whose clock later moves backwards allocates
        below a nonce already used, and every write is refused for a reason the caller cannot see.

        So an unreadable ledger is refused and left where it is, and "unreadable" includes a
        file that parses but does not hold the shape this writes. That is the rule I wrote for consumers
        elsewhere — a failed read is unknown, not empty, and the only safe action on unknown is
        none — applied to my own state file, which is where it had not been.
        """
        if not self._path.exists():
            if self._key_exists():
                raise self._refuse(
                    "is missing, and this key already exists, so a ledger was written and has "
                    "since been lost"
                )
            # Reset both, or this store keeps reporting the marker of a file it no longer has.
            # `records()` and anything else asking about marker presence would answer about the
            # previous read rather than the current state of disk (own audit).
            self._marker = {"v": _FORMAT_VERSION}
            self._marker_present = False
            return {}
        try:
            raw = json.loads(self._path.read_text(), object_pairs_hook=_no_duplicate_keys)
        except _DuplicateLedgerKeyError as exc:
            # `json.dump` cannot emit a duplicate key, so no file this class wrote can hold one:
            # it was hand-edited, or damaged by something merging two ledgers. That is unknown
            # state, not something to interpret, and it takes the same refusal as the rest.
            raise self._refuse(
                f"records {exc.key!r} twice in one object, so the parser kept one of the two "
                "values and discarded the other"
            ) from exc
        except (OSError, ValueError) as exc:
            raise self._refuse(f"could not be read as JSON ({exc.__class__.__name__})") from exc
        if not isinstance(raw, dict):
            raise self._refuse("does not hold a JSON object")
        # Take the initialisation record out before the entries are validated: it is not a DID
        # and must not be walked as one, and `records()` must not count it as history.
        marker = raw.pop(_MARKER, None)
        if marker is not None and not isinstance(marker, dict):
            raise self._refuse(f"holds {_MARKER!r} as something that is not an object")
        if marker is not None:
            # A marker written by a newer version may mean something this code does not know,
            # and "I do not understand this record" is the same unknown as a damaged one. The
            # version was unchecked when the marker was introduced, so a future v2 ledger would
            # have been read as a v1 one and quietly misinterpreted (own audit).
            version = marker.get("v")
            if isinstance(version, bool) or not isinstance(version, int):
                raise self._refuse(f"holds {_MARKER!r} with no usable version")
            if version > _FORMAT_VERSION:
                raise self._refuse(
                    f"was written in ledger format v{version}, and this build understands "
                    f"v{_FORMAT_VERSION}"
                )
        self._marker = dict(marker) if marker is not None else {"v": _FORMAT_VERSION}
        self._marker_present = marker is not None
        # Any ledger with no initialisation record, beside a key that already exists. Narrowing
        # this to the exact shape `{}` left `{"did:key:z...": {}}` accepted — no marker, but a
        # non-empty object, so the guard missed it and allocation fell back to the clock. The
        # property is "a file this package did not write", not "an empty file", and the code
        # claimed the former while checking the latter (own audit). Safe to broaden because this
        # package has never been released: there are no unmarked ledgers in the world to reject.
        # `initialise()` stamps every ledger it creates, so this file was not created by this
        # class — it was emptied. That is the same unknown as a corrupt one and takes the same
        # refusal. Without this, a ledger truncated to `{}` read as "initialised and never
        # used": allocation restarted from the clock, and on a host whose clock had moved
        # backwards it issued below a nonce already spent, so every signed write was refused
        # until wall time caught up (@Minh3132, #803).
        #
        # The legitimate never-signed state is not caught here. A first run creates the ledger
        # before the key and stamps it, so an interrupted one leaves the marker present with no
        # entries, which is exactly what this admits.
        if marker is None and self._key_exists():
            raise self._refuse(
                "carries no initialisation record, and this key already exists, so it was not "
                "written by this package — a stamped ledger has been replaced or emptied"
            )
        # Strict, and every violation is fatal rather than filtered. The previous version dropped
        # a malformed entry and carried on, which reads like leniency and is not: dropping an
        # entry does not omit a fact, it *asserts* that the pair has never issued a nonce. That
        # is the same claim the quarantine above exists to refuse, made one level in — and after
        # a clock rollback it resumes allocation below the server's replay floor exactly as if
        # the whole file had been lost (@yukkie3276, #803).
        for did, rooms in raw.items():
            if not isinstance(rooms, dict):
                raise self._refuse(f"maps {did!r} to something that is not an object")
            for room, nonce in rooms.items():
                # bool before int: `isinstance(True, int)` is true in Python, and `true` in the
                # file would otherwise load as the nonce 1.
                if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce < 0:
                    raise self._refuse(
                        f"holds {nonce!r} for {did!r}/{room!r}, which is not a non-negative integer"
                    )
        return {did: dict(rooms) for did, rooms in raw.items()}

    def _refuse(self, why: str) -> Exception:
        """Refuse to allocate, and leave the damaged ledger exactly where it is.

        The first version of this renamed the file aside to preserve it as evidence. That made
        the refusal last one process: the next `NonceStore` found the canonical path absent,
        read it as a first run, and allocated from the clock with the previous floor still
        unknown — the very failure the refusal exists to prevent, delayed by one restart
        (@yukkie3276, #803).

        Leaving it in place is both the evidence and the lock. Every process that starts reads
        the same unreadable file and refuses the same way, until a person removes or repairs it —
        and a person doing that has decided the clock is safely past whatever was issued, which
        is exactly the judgement no automatic recovery can make.
        """
        cost = (
            "The nonces already issued for this key are unknown, so allocating from the clock "
            "could repeat one and every signed write would be refused. "
        )
        # Two different recoveries, and giving the wrong one is not a cosmetic problem: the
        # advice for a damaged file is to repair it or move it aside, and neither is possible
        # for a file that is not there. An operator who follows it looks for something to move,
        # finds nothing, and has no route out of a refusal that repeats on every start. Same
        # lesson as the chained-refusal fix earlier on this branch — a true message can still
        # tell someone to do something impossible (own audit, after @yukkie3276's live-predicate
        # finding).
        if self._path.exists():
            advice = (
                "This file is left in place deliberately: it is the only record of what was "
                "issued, and while it is here no process will allocate. Recover by repairing it, "
                "or by moving it aside once you are satisfied the clock is past the last nonce "
                "used — or by using a different key."
            )
        else:
            advice = (
                "There is nothing here to repair or move aside. Restore the ledger from a backup, "
                "or use a different key. Creating a fresh one is safe only once you are certain "
                "the wall clock is past every nonce this key has ever used, which is a judgement "
                "no automatic recovery can make — so this refuses rather than making it for you."
            )
        return LedgerUnreadableError(why, f"{self._path} {why}. {cost}{advice}")

    def _lock(self):
        """Exclusive lock over the whole read-modify-write.

        A sidecar file rather than the state file itself: the state file is replaced by
        `os.replace`, so a lock held on it would follow the old inode and stop excluding
        anyone the moment the first writer finished.
        """
        mkdir_durable(self._path.parent)
        fd = os.open(str(self._path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _flush(self) -> None:
        mkdir_durable(self._path.parent)
        # `mkstemp`, not a pid-derived name. The pid is unique across processes and shared by
        # threads, and nothing on this method said it must be called under the lock — so one
        # caller flushing outside it would give two threads the same path to write and rename in
        # turn, tearing the file before `os.replace` ever sees it. `keyring.py` learned this and
        # moved to `mkstemp`; the ledger kept the pid (own audit). Latent rather than live: all
        # callers do hold the lock today.
        fd_tmp, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd_tmp, "w", encoding="utf-8") as handle:
            # The marker is written on every flush, not only at creation: it is what separates
            # this file from one truncated to `{}`, and a rewrite that dropped it would erase
            # that distinction for the next process to start.
            json.dump({_MARKER: self._marker, **self._state}, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
        # The rename itself needs to be durable, not just the bytes it points at.
        fsync_dir(self._path.parent)

    def identities(self) -> tuple[str, ...]:
        """Every DID this ledger records, named rather than counted, from one read.

        `records()` counts, and a count cannot answer the question `Signer` has to ask: whether
        the identities in this ledger include the one the local key derives. A ledger recording
        only somebody else's DID is a wrong seed restored or a home reused, and both look
        identical to `records()` — one key, some allocations, nothing wrong.

        The judgement stays with `Signer` and cannot move here. A store constructed directly may
        hold several DIDs and sign for all of them legitimately, so "none of these is mine" is
        only a defect for a caller that knows there is exactly one key at this home.
        """
        return tuple(self._reload())

    def last(self, did: str, room: str) -> int | None:
        """The last nonce allocated for this pair, or None if there has never been one.

        Reloaded rather than read from memory. It used to answer from `self._state`, which is
        whatever the last load happened to leave there — so a store still open across another
        process's allocation reported a number that process had already passed, and a caller
        deciding anything from it was reading history as the present. Every other read in this
        class reloads, `_allocate_locked` explains at length why the in-memory copy cannot be
        trusted, and this was the one exception; no caller depended on the stale answer (own
        audit).
        """
        return self._reload().get(did, {}).get(room)

    def _reload(self) -> dict[str, dict[str, int]]:
        """Re-read the ledger under the lock, for the paths that only look.

        The same reload `_allocate_locked` does, without the write: taking the lock for a read
        means the answer is not one an allocation already holding it has decided to supersede.

        Never called from inside the lock. `flock` attaches to the open file description and
        `_lock` opens a fresh one per call, so a nested read would block on a lock this same
        process holds — a deadlock rather than a re-entry. `allocate` is the only holder, and it
        reloads itself rather than calling this.
        """
        fd = self._lock()
        try:
            self._state = self._load()
            return self._state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def allocate(self, did: str, room: str) -> int:
        """Reserve and persist the next nonce for `did` in `room`, then return it."""
        global _process_floor
        fd = self._lock()
        try:
            return self._allocate_locked(did, room)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _allocate_locked(self, did: str, room: str) -> int:
        global _process_floor
        # Re-read inside the lock. The copy loaded at construction is stale the moment another
        # process allocates, and allocating from it is exactly the duplicate this lock exists to
        # prevent — taking the lock and then trusting memory would be a lock that guards nothing.
        self._state = self._load()
        known = self._state.get(did, {}).get(room)
        # With no record, the floor is the clock PLUS ONE, not the clock. A nonce equal to
        # the current millisecond may already have been used and forgotten — that is exactly
        # the state after a lost file — and the server's rule is strictly greater, so
        # returning `now` would be refused with nothing local to explain it. The test for
        # this failed before the `+ 1` was here.
        # One read, used for both the fallback and the floor. Reading the clock twice meant a
        # millisecond boundary falling between them returned a nonce equal to the clock — exactly
        # the value the `+ 1` above was added to avoid, refunded silently (own audit).
        now_ms = int(time.time() * 1000)
        previous = known if known is not None else now_ms
        # `max` and not `previous + 1`: a clock that has moved forward since the last write
        # jumps the counter, which keeps two processes sharing one key from colliding on a
        # value neither has persisted yet.
        nonce = max(now_ms, previous + 1, _process_floor + 1)
        _process_floor = nonce
        self._state.setdefault(did, {})[room] = nonce
        self._flush()
        return nonce
