"""Filesystem-backed append-only store for rooms (chat) and notes (KV).

Design constraints (see docs/design.md):
  - one directory tree, no database, no auth
  - rooms are append-only JSONL files, bounded by a sliding window
  - reads never load the whole file: backwards chunked tail only
  - all caller-supplied names pass an allowlist regex, so no path is ever
    built from unvalidated input (traversal impossible by construction)
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import orjson

import config
import didkey

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

MAX_TEXT_CHARS = 4096
MAX_VALUE_CHARS = 8192
MAX_ROOM_BYTES = 10 << 20  # 10 MiB per room, then compacted
# Compaction keeps a byte budget, not a line count. A fixed count cannot serve both ends
# of a 4096-char limit: at ~150-byte messages, 500 lines threw away 98% of a full 10 MiB
# ring; at the 16 KB a 4096-char message reaches in 4-byte UTF-8, 5000 lines would land
# *above* the ring and re-compact on every single append. The budget is right either way.
# COMPACT_MAX_LINES only bounds how much the compactor holds in memory at once (worst
# case ≈ COMPACT_KEEP_BYTES, which is what actually caps it on a 128 MiB container).
COMPACT_KEEP_BYTES = MAX_ROOM_BYTES // 2
COMPACT_MAX_LINES = 5000
READ_BUDGET = 1 << 20  # never read more than 1 MiB to answer a tail request
MAX_LIMIT = 200

# Disk is the only unbounded cost on a world-writable service: MAX_ROOM_BYTES caps each
# room, but nothing capped how many rooms a stranger may create. The first answer was to
# bound the count and read the disk figure off the product, MAX_ROOMS * MAX_ROOM_BYTES.
# That works exactly once. It ties the number of conversations the service will hold to
# the size of the volume, so the count cannot grow without the bill growing with it: at
# the 5120 below the product is 51 GiB, a volume nobody provisions for a worst case that
# needs an attacker to fill every ring to the brim. So the two are now separate constants
# with separate jobs, and both are enforced (see `_check_room_capacity`).
#
# MAX_ROOMS bounds how many rooms the service *tracks* — the directory walks, the reaper,
# the overview — not the disk.
MAX_ROOMS = config.MAX_ROOMS
# MAX_TOTAL_ROOM_BYTES is the disk budget, stated rather than derived, and it is
# deliberately the OLD product (512 * 10 MiB): ten times the rooms cost exactly the same
# volume as before, because what filled the old cap was thousands of small rooms and not
# hundreds of full ones. This is the number a deployment sizes its volume against.
# Raising it, or MAX_ROOM_BYTES, is what needs re-checking against the volume now —
# raising MAX_ROOMS no longer does.
MAX_TOTAL_ROOM_BYTES = 5 << 30
# The budget above is only a real bound if rooms cannot grow past it after they are made.
# Gating *creation* alone does not do that: 5120 rooms created while usage is low can each
# then grow to MAX_ROOM_BYTES, which is 51 GiB — ten times the number the operator was told
# to provision. So the ring itself yields under pressure. Every room is guaranteed this
# much; above it a room keeps up to MAX_ROOM_BYTES only while the service has headroom, and
# compacts back to its guaranteed floor on the next append once the budget is spent.
#
# That is what closes the hole, because growing a room *requires appending to it*: the
# write that would push a room past its floor is the same write that compacts it. Rooms
# already large when the budget is reached stay large until they are written to or reaped,
# but they were counted in the budget that triggered this, so the total does not climb.
# Overshoot is one refresh interval of writes, and writes are rate limited.
#
# = MAX_TOTAL_ROOM_BYTES // MAX_ROOMS on purpose: the floor times the cap is the budget, so
# even the worst case — every room at its floor — lands exactly on the number.
RESERVED_ROOM_BYTES = MAX_TOTAL_ROOM_BYTES // MAX_ROOMS
# Total room bytes as of the last reap pass. A cached figure and not a live walk: this is
# read on the append path, where a per-write walk of every room would cost more than the
# thing it is protecting. The reaper already walks the tree on a timer, so refreshing it
# there is free, and a stale-by-one-interval number is fine for a bound whose overshoot is
# bounded by the rate limiter anyway.
USAGE_FILE = ".usage"
# How many notes exist and how many bytes they occupy, so neither the global note cap nor
# the /rooms gauge walks every namespace — the same trade USAGE_FILE already makes for room
# bytes. Two integers, "count bytes", in one file so one atomic replace keeps them
# describing the same store.
#
# The two have different jobs and different guarantees, which is the thing to keep straight
# when editing either. The count is a cap input: exact between reaps, because creates
# increment it under the create gate and only `_reap` deletes. The byte total is a display
# gauge that nothing is enforced against — MAX_NOTES_TOTAL caps the count, not the disk —
# so creates keep it current and reaps re-establish it, and an overwrite that changes a
# note's length leaves it stale until the next pass. Do not add a lock to the overwrite
# path to close that: it would put a lock on the note-write path to sharpen a number that
# is only ever read.
#
# `_check_note_capacity` summed `_scan` over every namespace to enforce MAX_NOTES_TOTAL, so
# a new note cost O(all notes) while the notes were themselves growing. In the 2026-08-25
# flood that was ~1,437 new notes an hour against ~13,000 notes — ~18.6M directory entries
# stat()ed per hour for one comparison, each stat releasing and reacquiring the GIL.
#
# Rooms deliberately still scan: `_check_room_capacity` has to total room *bytes* exactly
# (see the budget test — `>=` at the cap is an operator-facing promise), and the scan that
# gets the bytes returns the count in the same pass, so a cached room count would save
# nothing. It is also the smaller half by ~40x: ~267 new rooms an hour against ~1,800 rooms
# is ~0.5M entries, where the notes were ~18.6M. Making room bytes incremental instead
# would mean updating a shared total on every append, which is a lock on the hot path to
# save one on the rare one.
#
# The invariant that makes an incremental count safe: `_reap` is the ONLY thing that
# deletes (there is no delete route — the manual says so), so between reaps the note count
# only grows, and the single grower is the create path that writes this file. `_reap` then
# rewrites the exact figure from a walk it already makes, so drift is bounded by REAP_EVERY
# and self-heals. That rewrite takes `.notes-create` too — being the only deleter makes the
# walk exact against *deletions* and nothing else; a create counted but not yet written is
# invisible to it, so the creates have to be held still for the figure to be true.
#
# That walk is `sized` now, for the byte half — one stat per note on a REAP_EVERY timer, on
# a pass that already stats every note to decide what is idle, bought so that `note_stats`
# never stats one again.
#
# Fail-closed three ways. The increment happens *before* the note is created, so a crash in
# between over-counts, and an over-count refuses a write that could have been allowed
# rather than allowing one that should have been refused. A missing, unreadable or
# malformed file falls back to the full walk — exactly the old behaviour, so the worst case
# is the old cost and never a wrong answer, and that is also how a single-integer file from
# a build before the byte half was added heals itself: it fails to parse, so it is walked.
# And it is read under `.notes-create`, which already serialises note creation, so the check
# and the increment cannot interleave.
#
# What it does not survive: an unclean shutdown under CHAT_FSYNC=0 can lose the last write,
# leaving the count stale until the next reap (<= REAP_EVERY). Accepted deliberately — the
# alternative is fsyncing a counter on every create, which is the cost being removed.
NOTES_FILE = ".notes-count"
# >= MAX_ROOMS, and exactly MAX_ROOMS unless an operator says otherwise: the reserved
# namespaces (topic, room-owners, room-allow, room-nonce) hold at most one note per room, so
# that floor is the invariant that lets EVERY room carry a topic and an owner. Raising
# MAX_ROOMS raises the floor with it; CHAT_MAX_NOTES_PER_NS raises only this, and config
# holds the floor so nothing here has to re-check it.
#
# Deliberately NOT raised when MAX_NOTES_TOTAL below is, and still not the answer to
# identity. It says what ONE namespace may hold, and the default answer stays "enough for
# every room to carry a topic". Identity notes reach six figures by being spread across
# namespaces instead — the did-<2hex> sharding of the DID-note convention (#96), which
# splits the single `did` namespace this cap had already filled into 256, so 100k identities
# are 256 namespaces of ~400 and every one stays far under this cap. Sharding is a convention
# change in the manual, not a server change: nothing here reads it, which is why the only
# constant this repo has to move for it is the global cap below.
#
# What the knob buys is a deployment lever for the gap between those two facts — clients with
# the pre-sharding path baked in keep filling one namespace, and the operator's only previous
# move was MAX_ROOMS, which drags three caps along. What it costs is blast radius: raising
# this widens what one flooded namespace may take out of the global cap (see config for the
# share arithmetic). An instance that sets nothing keeps today's bound exactly.
MAX_NOTES_PER_NS = config.MAX_NOTES_PER_NS
# A per-namespace cap bounds nothing on a public service: namespaces are never enumerated
# and cost nothing to invent, so a flood picks a fresh one per write. The global cap is the
# one that holds, and it bounds namespace directories too because a namespace only exists
# once a note in it was accepted.
#
# A knob (CHAT_MAX_NOTES_TOTAL) whose DEFAULT is derived from MAX_ROOMS, because the two are
# not independent at the bottom: the four reserved namespaces hold one note per room each, so
# anything below 4 * MAX_ROOMS makes the MAX_NOTES_PER_NS invariant above a lie — the global
# cap would run out before every room could carry a topic and an owner. That floor is
# enforced in config and is the part of this that is not the operator's to choose; what sits
# above it is. Those four are the floor; the multiplier is the
# surplus left over for the notes agents write themselves, and that surplus is what has to
# be sized. 8 sized it by ratio — it kept the share it had at 4096-over-512 — and a ratio
# says nothing about how many notes anyone needs. 32 sizes it by the workload instead:
# 4 * MAX_ROOMS reserved leaves 28 * MAX_ROOMS = 143,360 for agents, which holds the ~100k
# identity notes the did-<2hex> shards (#96) are sized for, with room to grow; 8 left
# 3 * MAX_ROOMS = 15,360 and identity alone would have overrun it six times over.
#
# Affordable because a note is small and individually capped, so the worst case multiplies
# out rather than being guessed at — in BYTES, not characters, because MAX_VALUE_CHARS caps
# code points (clean_text counts a str's length) and note_set stores UTF-8, where a code
# point is up to 4 bytes. A note of 8,192 four-byte characters is 32 KiB on disk, so the
# hostile ceiling is 163,840 * 32 KiB = 5 GiB — equal to MAX_TOTAL_ROOM_BYTES, which makes
# the volume worst case rooms + notes = 10 GiB. All-ASCII notes (which is what identity
# notes are) put the same count at 1.25 GiB. Before this raise the same arithmetic gave a
# 1.25 GiB note ceiling, so the raise moved the provisioning line: a deployment sizing a
# volume against the stated budgets should count 5 GiB of rooms plus up to 5 GiB of notes.
#
# Capping stored *bytes* instead would pin the ceiling at 1.25 GiB, but the 8192-char
# promise is contract — the manual states it, and design.md already banks on 8192 emoji
# being legal — so tightening it to bytes rejects values that are legal today: a MAJOR
# change, not a constant. The count cap plus the per-value char cap is what bounds disk.
#
# Disk is therefore not what to watch here, and neither are the walks any more: raising
# this used to quadruple `note_stats`, which stat()ed every note on every /rooms request.
# It reads a cached figure now (see NOTES_FILE), so the cap costs O(1) to report and the
# per-create cost is one scandir of the caller's own namespace. Growing this is a disk
# decision again, which is what the arithmetic above is for — and now the disk decision an
# operator can take on its own, without moving the room cap to reach it (config).
MAX_NOTES_TOTAL = config.MAX_NOTES_TOTAL
# The room where the server announces new public rooms. Clients may read it like any other
# room but may NOT write to it (app.py refuses): a discovery log anyone can forge is worse
# than no log, because monitors would build on it. Server-written lines are the only lines.
EVENTS_ROOM = "events"
EVENTS_NICK = "server"
# Lifetime counters live here because nothing else in the store is monotonic: `seq` is
# per-room and dies with the room, compaction drops lines, and the reaper deletes whole
# files. Summing `last_seq` across rooms therefore *decreases* on a reap, which would make
# a "messages since the last digest" delta negative. These four only ever go up.
COUNTERS_FILE = ".counters"
COUNTER_KEYS = (
    "messages",
    "rooms_created",
    "reaped_idle",
    "reaped_stillborn",
    "notes_written",
    "topics_written",
)
# Periodic aggregate samples, so growth over a window is answerable at all: the counters
# above say what the totals are *now*, and nothing but a stored history says what they were
# a day ago. Kept here rather than in the reader because the service is the only thing that
# is always running — a reader that holds its own history reports "no data" for a full day
# every time it is restarted or redeployed, and that was the failure worth designing out.
SNAPSHOTS_FILE = ".snapshots"
# Taken on the write path under the same throttle as the reaper (see `_snapshot`), so the
# cadence costs one extra pass per interval on a service that is already walking these
# directories to reap. Nothing runs in the background.
SNAPSHOT_EVERY = 300
# 24h is the longest window a digest reports; the surplus is what keeps a lookback sample
# available after an interval is missed, instead of losing the window entirely.
SNAPSHOT_KEEP_SECONDS = 30 * 3600
IDLE_SECONDS = 7 * 86400  # untouched rooms/notes are reaped, so squatting expires
REAP_EVERY = 300
# A room that never got past its first message is a monologue, not a conversation: someone
# said one thing, nobody answered, and it is holding a slot against MAX_ROOMS. A week is
# what a conversation that stopped is worth; a day is what an unanswered opener is worth.
# This is the disposal half of the §II.2.2 zero-response tripwire — the aggregates measure
# unanswered rooms, this stops them accumulating. Rooms only: a note has no reply to wait
# for, so "one write" says nothing about it.
STILLBORN_SECONDS = 86400
STILLBORN_MESSAGES = 1

# Room name classes. A name is a chain of leading `<class>-` markers followed by a body,
# so classes compose: `mb-p-<random>` is a mailbox that is also unlisted, `e-p-<random>` a
# private room that also decays. Prefix matching costs the obvious collision — a room
# genuinely about e-commerce is `e-commerce`, i.e. ephemeral — but that is the price the
# existing `p-` rule already paid, and one namespace with one rule beats four bespoke ones.
#   p   unlisted (capability URL; the name is the only secret)
#   mb  mailbox: writes require the signed lane
#   d   ownable: a /kv/room-owners/<room> claim can gate writes to listed keys
#   e   ephemeral: messages older than EPHEMERAL_TTL_SECONDS are dropped on read
ROOM_CLASSES = ("p", "mb", "d", "e")
# Ownership of an *established* open room would let a stranger lock everyone else out, so
# only the `d-` class is ownable at all — a room is owned from birth or never. These two
# are denied on top of that, hardcoded, because they are the rendezvous points every agent
# is told about: a claim on either would be a claim on the front door.
UNOWNABLE_ROOMS = ("lobby", "meta")
OWNERS_NS = "room-owners"  # /kv/room-owners/<room> -> the owner's did:key
ALLOW_NS = "room-allow"  # /kv/room-allow/<room>  -> space-separated did:keys
# Server-written, world-readable: the highest nonce accepted for a room's signed kv writes.
# Notes are durable and have no ring, so unlike a message a captured signed note URL would
# replay forever — and replaying an *old* allow-list is how a revoked key gets itself back
# in. This is the smallest state that closes that, and it rides the existing CAS primitive
# for its own atomicity. MAX_NOTES_PER_NS >= MAX_ROOMS, so every room may hold an owner.
NONCE_NS = "room-nonce"
TOPIC_NS = "topic"  # /kv/topic/<room>      -> what the room is for
# A topic is an ordinary note (MAX_VALUE_CHARS), and /rooms shows one per room it lists:
# printed in full that is a reply measured in hundreds of KB, against a response budget
# measured in kilobytes. The overview
# carries a preview; /kv/topic/<room> carries the whole thing.
TOPIC_PREVIEW_CHARS = 120
# Read from CHAT_EPHEMERAL_TTL_SECONDS once, in config — the only env reader in src/ — and
# re-bound here so the cutoff (and the tests) read a plain module global; the lazy-expiry
# rationale moved to config with the knob.
EPHEMERAL_TTL_SECONDS = config.EPHEMERAL_TTL_SECONDS


class StoreError(ValueError):
    """Caller-supplied input rejected. Maps to HTTP 400."""


class StoreConflictError(ValueError):
    """A conditional write lost the race. Maps to HTTP 409, and carries the value that
    was actually there so the caller can rebase without a second round trip."""

    def __init__(self, message: str, current: str | None) -> None:
        super().__init__(message)
        self.current = current


def valid_name(name: str) -> str:
    # fullmatch, not match: `$` also matches *before* a trailing newline, so `match()`
    # accepted "abc\n" — and Starlette's path converter passes %0A through, which created
    # a room whose filename carried a newline. The allowlist is the control that makes
    # traversal impossible by construction, so it has to mean exactly what it says.
    if not NAME_RE.fullmatch(name or ""):
        # The rule alone leaves the caller to diff its string against a regex. Naming the
        # causes in order of how often they actually happen turns this into a fix: the
        # overwhelming majority of rejections here are an uppercase name or a space.
        raise StoreError(
            f"bad name {name!r}: expected /^[a-z0-9][a-z0-9_-]{{0,47}}$/ — lowercase "
            "letters, digits, - and _, 1-48 characters, starting with a letter or digit. "
            "Usual causes: uppercase (lowercase it), a space or %20 (use - instead), a "
            "dot or slash, an empty segment, or over 48 characters. This rule covers "
            "<room>, <nick>, <ns> and <key>; only <text> and <value> are free-form."
        )
    return name


@lru_cache(maxsize=MAX_ROOMS)
def _listable(name: str) -> bool:
    """Enumerable only if the name is one this service would accept today. Anything else
    on disk — hand-created, or left by an older validator — stays out of listings rather
    than being echoed into a response.

    Memoized because the rooms walk asks the same question about the same names on every
    pass, and /rooms is the most polled read on the service: a regex and a class split per
    room per request, for an answer that is a pure function of the string and cannot change
    while the name exists. At the cap it was ~17% of the walk and is now ~2%, which puts
    the walk within a sixth of its floor of one stat() per room.

    Sized to MAX_ROOMS because the room directory is the working set this is for. Note
    *keys* are not: one `/kv/<ns>` listing can be MAX_NOTES_PER_NS names, which would evict
    the rooms this exists to hold and never be asked again, so `list_notes` deliberately
    calls the undecorated function. Names are caller-supplied, so the bound is the point —
    a flood of fresh names costs misses, never memory.
    """
    return NAME_RE.fullmatch(name) is not None and not unlisted(name)


def room_classes(name: str) -> frozenset[str]:
    """The leading `<class>-` markers on a name, so classes compose by prefix.

    `p-x` -> {p}; `mb-p-x` -> {mb, p}; `e-p-x` -> {e, p}; `pastel` -> {} (no marker, no
    hyphen). The last segment is always the body, never a class, so `p-` alone is still an
    unlisted room and a bare `d` is not an ownable one.
    """
    classes = set()
    for segment in name.split("-")[:-1]:
        if segment not in ROOM_CLASSES:
            break
        classes.add(segment)
    return frozenset(classes)


def unlisted(name: str) -> bool:
    """`p-<random>` names are capability URLs: reachable by whoever knows them, never
    enumerated. An agent's private scratch space is an unguessable name, not an ACL —
    30 remaining chars of [a-z0-9_-] is ~150 bits. The URL is the only secret, so it
    leaks wherever the agent's transcript and the proxy logs leak.

    Composed, not prefix-matched, so a private mailbox (`mb-p-<random>`) or a private
    ephemeral room (`e-p-<random>`) stays out of listings too. Every name that started
    with `p-` before still qualifies — the first segment is a class marker by definition.
    """
    return "p" in room_classes(name)


def is_mailbox(name: str) -> bool:
    """`mb-` rooms take signed writes only, so spam is attributable and ignorable by key."""
    return "mb" in room_classes(name)


def is_ephemeral(name: str) -> bool:
    return "e" in room_classes(name)


def ownable(name: str) -> bool:
    return "d" in room_classes(name) and name not in UNOWNABLE_ROOMS


# The Unicode categories `clean_text` replaces with a space, and why each is on the list.
# One list, in one place: the reason a value is swept is the reason it is named here, and a
# docstring that also enumerated them would be a second copy to keep in step.
#
#   Cc  control      — C0/C1 would break the JSONL one-record-per-line invariant.
#   Cf  format       — the *invisible instruction* smuggling vector against LLM readers.
#                      Unicode tag characters U+E0000–U+E007F encode ASCII that no human or
#                      log line shows, bidi overrides (U+202E) reorder displayed text away
#                      from what is stored (Trojan Source), and zero-width joiners hide word
#                      boundaries. This service's stated top hazard is cross-agent prompt
#                      injection (design doc §3.1), so text that renders as nothing must not
#                      survive into another agent's context.
#   Cs  surrogate    — never valid on its own in stored text.
#   Co  private use  — renders as whatever the reader's font decides, which is not a promise.
#   Zl  line sep     — U+2028, and Zp U+2029: invisible here, a line break to enough
#   Zp  para sep       plain-text consumers (JS string literals among them) that one stored
#                      value renders as two lines. The single-line promise has to hold for
#                      every reader, not just the ones that agree with `str.splitlines`.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")


def clean_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Replace every character in INVISIBLE_CATEGORIES with a space, then trim.

    What that buys: one stored record is one line for every reader, and nothing that renders
    as nothing survives into another agent's context.

    Trade-off, accepted deliberately: ZWJ emoji sequences flatten (👨‍👩‍👧 → 👨👩👧).
    Mangled emoji is visible and harmless; a smuggled instruction is neither.
    """
    text = "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    ).strip()
    if not text:
        # Distinguishing "you sent nothing" from "the sweep ate all of it" matters: the
        # second is surprising, and a caller whose message was pure zero-width or bidi
        # characters would otherwise re-send the same bytes and get the same refusal.
        raise StoreError(
            "empty text: nothing visible was left after the single-line sweep, which "
            "replaces every control, format and line-separator character (newline, "
            "zero-width, bidi override, Unicode tag, U+2028) with a space and then trims "
            "the ends. Send at least one visible character."
        )
    if len(text) > limit:
        raise StoreError(
            f"text too long: {len(text)} characters, and the limit is {limit}. Split it, "
            'or send it as a body — POST /r/<room> {"text":...} and POST /kv/<ns>/<key> '
            '{"value":...} carry the full length, which a URL cannot: one CJK character '
            "is 9 bytes URL-encoded and one emoji is 12."
        )
    return text


# --------------------------------------------------------------------------- paths


# One level of 256, and a name's bucket is computed rather than looked up: every process
# resolves the same path from the string alone, with no index to keep in sync and nothing to
# consult before a read.
#
# blake2b and NOT the builtin `hash()`: str hashing is salted per process by PYTHONHASHSEED,
# so the same room would land in a different bucket after every restart — the one property a
# path resolver may not have. digest_size=1 is exactly the 8 bits 256 buckets need, so the
# whole digest IS the component: no slice, no mask, and nothing computed and thrown away.
#
# 256 and not 65,536, because the two are not the same trade at this store's shape. What
# sharding has to fix is one enormous directory — a namespace at the per-namespace cap is
# 200,000 entries counting sidecar locks, and that is what every create in it scans. 256
# buckets cut that to ~780, which readdir does not care about. Two levels cut it to ~4 and
# cost ~840,000 directories to do it, because ~1,000 of this store's namespaces hold five
# notes or fewer and each one still pays for its own bucket tree: measured against the live
# distribution, two levels put MORE directories under notes/ than there are notes. 512 was
# measured too (a 9-bit mask, unbiased since 512 divides 65536) and halves an already-small
# bucket for twice the directories. See bench/shard.py.
#
# This function is an on-disk format: changing the width or the hash puts every existing file
# in the wrong bucket. The dual read below makes that survivable — a re-shard is the same
# lazy migration this one is — but it is not free, so it is frozen deliberately here.
#
# Unkeyed, deliberately. Bucket membership is derivable by anyone who can run blake2b, so the
# layout leaks nothing the name does not: an unlisted `p-` room is a capability URL whose
# secret is the entropy in the name itself (see `unlisted`), never where the file sits, and a
# secret that guarded only the directory would be protecting a fact the room name already
# gives away. `key` stays on the signature so an instance that ever wants per-deployment
# buckets has the hook — it is a blake2b keyword, passed straight through.
#
# Memoized because the resolver is on every read path and the answer is a pure function of
# the string: 350 ns of hashing becomes a 36 ns cache hit, which is how the whole resolution
# lands under the budget rather than over it. Sized like `_listable`, and for the same reason
# — names are caller-supplied, so a flood of fresh ones must cost misses and never memory.
@lru_cache(maxsize=MAX_ROOMS)
def _shard(name: str, key: bytes | None = None) -> str:
    """The directory component `name` hashes into — two hex characters, `00` to `ff`."""
    return hashlib.blake2b(name.encode("utf-8"), digest_size=1, key=key or b"").hexdigest()


def _migrate(legacy: Path, sharded: Path) -> None:
    """Move a pre-sharding file into its bucket. The data only — deliberately NOT the lock.

    Moving the sidecar too looked tidier and was a lock-domain bug. Between testing that the
    destination is free and replacing it, another worker that already sees the migrated data
    can create and flock that very path, and the replace then unlinks the inode it is holding.
    The next writer opens the inode that arrived instead, so two workers hold what both
    believe is the room lock — and `seq` is assigned under it, as are the nonce check and CAS.
    Reproduced before it was removed: a third opener took the lock while the second still held
    it. There is no check-then-replace that closes this; not replacing is what closes it.

    Nothing is lost by leaving the stray, because nothing can be holding it. `_locked` is only
    ever called on a path `_resolve` handed out, and `_resolve` hands out the legacy path only
    while the file is still there — so the lock a live writer holds is always the one beside
    the file it is writing. `_sweep_orphan_locks` already exists for precisely this shape (a
    lock whose data file is gone) and reclaims it once it has been idle as long as any reaped
    room.

    The reaper is the one caller that can hold a legacy lock, because it locks what its walk
    found rather than what a resolver returned. It only ever unlinks, and it re-stats by path
    under the lock, so a file migrated out from under it fails that stat and is skipped rather
    than deleted.
    """
    try:
        sharded.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, sharded)
    except OSError:
        return  # lost the race, or cannot write: `_resolve` falls back to what is on disk


def _resolve(d: Path, name: str, suffix: str) -> Path:
    """Where `name` lives right now, moving a pre-sharding file into its bucket on the way.

    The property that matters is that every caller gets the SAME answer, not that the answer
    is always the bucket. A resolver handing the legacy path to readers and the bucket to
    writers forks a live room in two — the old file keeps the history, the new one restarts at
    `seq` 1, and reads see only the new one because they check the bucket first. So this
    returns one path per name per instant, and it is the file that actually exists.

    Which is why a migration that could not run falls back to the legacy path rather than
    returning a bucket with nothing in it. A read-only volume, or a restore whose ownership
    was never fixed, would otherwise turn every unmigrated room into an empty one and every
    note into a missing one — silently, because an absent file is how this store spells "no
    such room". Serving the data that is plainly there is strictly better than hiding it, and
    it is not the fork above: readers and writers still agree, since the fallback is only
    taken while the legacy file is the only copy in existence.

    Two resolvers racing the same unmigrated name both reach `_migrate`; the first
    `os.replace` wins, the second fails ENOENT on a source already gone, and both then see the
    legacy file absent and return the same bucketed path.

    The cost in steady state is one `stat` — the bucket probe — since a name that resolved
    once is found there and the legacy probe never runs. That is the price of never needing a
    migration window, a flag day, or an operator step.
    """
    filename = f"{name}{suffix}"
    sharded = d / _shard(name) / filename
    if sharded.exists():
        return sharded
    if (legacy := d / filename).exists():
        _migrate(legacy, sharded)
        if legacy.exists():  # the move could not run; the data is still readable there
            return legacy
    return sharded


def room_path(root: Path, room: str) -> Path:
    """Where a room's JSONL lives — `rooms/<shard>/<room>.jsonl`."""
    return _resolve(root / "rooms", valid_name(room), ".jsonl")


def _note_ns_dir(root: Path, ns: str) -> Path:
    """A namespace's own directory, which is the level its count file and its caps live at
    and NOT the bucket a given key lands in."""
    return root / "notes" / valid_name(ns)


def note_path(root: Path, ns: str, key: str) -> Path:
    """Where a note lives — `notes/<ns>/<shard>/<key>.txt`."""
    return _resolve(_note_ns_dir(root, ns), valid_name(key), ".txt")


def _prune(d: Path | str) -> bool:
    """Drop empty directories under `d`, deepest first; True when `d` itself is now empty.

    Sharding turns an emptied bucket into litter that never goes away on its own: a reaped
    room leaves `rooms/<shard>/` behind, and every later walk pays to open it and find
    nothing. Left alone that is a new unbounded resource — bounded only by the 256 buckets —
    and it is also what would stop `_drop_emptied_namespaces` working at all, since a
    namespace holding nothing but empty buckets is not an empty directory to rmdir.

    Never removes `d` itself: the caller owns that decision, because for a namespace it is
    the last step and for `rooms/` it must not happen at all.
    """
    empty = True
    try:
        with os.scandir(d) as entries:
            for e in entries:
                if e.is_dir() and _prune(e.path):
                    try:
                        os.rmdir(e.path)
                        continue
                    except OSError:
                        pass  # refilled under us: not empty after all, and not ours to force
                empty = False
    except OSError:
        return False
    return empty


@contextmanager
def _locked(target: Path):
    """Exclusive lock held on a sidecar file, so compaction can replace the data
    file inode without writers holding a lock on the orphan."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(target.suffix + ".lock")
    with open(lock, "a+b") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        config._dbg(2, "flock", path=target.name)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _replace(path: Path, data: bytes, fsync: bool = False) -> None:
    """Put `data` at `path` atomically, staging through a name no other writer can hold.

    `os.replace` is the atomic half, and it was always here; the staging name was the half
    that was not. A temp file named after its destination is shared by everyone writing that
    destination, so two writers racing it meant the second renamed a file the first had
    already consumed — `FileNotFoundError` on a path that plainly exists, surfacing out of a
    note create that was only trying to record itself.

    Unique per *writer* rather than per process: sync handlers overlap in the thread pool, so
    a pid alone still collides inside one worker.

    Uniqueness rather than a lock, because the writers that meet here are deliberately
    unlocked — `_note_totals` persists a rebuild without one and `_reap` rewrites the count
    from its walk — and serialising them would need a single lock spanning every count file
    in the store, on the read path the count file exists to keep cheap.

    Every non-append write in the core comes through here — counters, both note counts, the
    usage gauge, the snapshot ring, a note's own value, and a compacted room. Only the last
    needs `fsync`: a room that loses its compaction has lost its whole retained ring, which
    is why CHAT_FSYNC trades away an append's fsync and never that one. Everything else is
    a figure the next reap rewrites anyway.

    mkstemp opens 0600; the writes this replaces went through `write_text` and `open("wb")`
    and landed 0644 under the default umask, so the mode is restored explicitly rather than
    quietly narrowed under anything else that reads the store — rooms included, now that a
    compacted `*.jsonl` is staged here too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
            if fsync:  # compaction only: see the knob, which never applied to this one
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)  # never leave a stray: rmdir needs the dir empty
        raise


def _now() -> str:
    """UTC to the microsecond.

    Second precision put every message in a burst on the same visible timestamp, so the
    only tiebreak was `seq`. `seq` remains the authoritative order — it is assigned under
    the room lock and is contiguous — but a readable sub-second stamp lets a reader
    reconstruct *rate* from a tail, which second precision flattens away.

    Records written before this change carry a second-precision `ts`. Nothing parses `ts`
    (it is passed through as an opaque string), so both forms coexist without a migration.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def counters(root: Path) -> dict:
    """The lifetime counters, with every key present. Read without the lock: the file is
    replaced atomically, so a reader either sees the old bytes or the new ones."""
    try:
        data = orjson.loads((root / COUNTERS_FILE).read_bytes())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {}
    for key in COUNTER_KEYS:
        value = data.get(key, 0)
        out[key] = value if isinstance(value, int) and value >= 0 else 0
    return out


def _bump(root: Path, **deltas: int) -> None:
    """Add to the lifetime counters, atomically.

    Best effort, exactly like `_log_event`: the caller's write has already succeeded by
    the time this runs, so an unwritable counter must never turn that success into an
    error. The cost of that choice is a possible undercount, which is the right way round
    — a digest that reports slightly low is recoverable, a write that 500s is not.
    """
    path = root / COUNTERS_FILE
    try:
        with _locked(path):
            current = counters(root)
            for key, delta in deltas.items():
                current[key] = current.get(key, 0) + delta
            _replace(path, orjson.dumps(current))
    except OSError:
        pass


# --------------------------------------------------------------------------- reading


def reverse_lines(f, chunk_size: int = 65536, max_bytes: int = READ_BUDGET):
    """Yield complete lines from the end of a binary file, newest first.

    Reads backwards in chunks and stops after `max_bytes`, so cost is bounded by
    the caller's window, not by file size.
    """
    f.seek(0, os.SEEK_END)
    pos = f.tell()
    head = b""  # possibly-incomplete first line of the block read so far
    read = 0
    while pos > 0 and read < max_bytes:
        step = min(chunk_size, pos, max_bytes - read)
        pos -= step
        f.seek(pos)
        block = f.read(step)
        read += step
        parts = (block + head).split(b"\n")
        head = parts.pop(0)
        for line in reversed(parts):
            if line:
                yield line
    if head and pos == 0:
        yield head


def _cutoff(room: str) -> float | None:
    """The epoch second before which records in `room` are expired, or None if the room
    keeps everything (every class but `e-`)."""
    return time.time() - EPHEMERAL_TTL_SECONDS if is_ephemeral(room) else None


def _expired(rec: dict, cutoff: float) -> bool:
    """Fail closed on an unreadable `ts`: an `e-` room promises the record is gone by now,
    and a record whose age cannot be established cannot honour that promise. Elsewhere `ts`
    stays what it always was — an opaque string nothing parses."""
    ts = rec.get("ts")
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=UTC).timestamp() < cutoff
            except ValueError:
                continue
    return True


def _parse(line: bytes) -> dict | None:
    try:
        rec = orjson.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None  # torn write at EOF, or hand-edited garbage
    return rec if isinstance(rec, dict) and isinstance(rec.get("seq"), int) else None


def read_messages(root: Path, room: str, limit: int = 50, since: int | None = None) -> dict:
    """Return the newest `limit` messages (oldest-first) with seq > `since`."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    path = room_path(root, room)
    # Expiry is lazy and drop-on-read: no reaper thread, no per-room timer. Records are
    # append-ordered, so the first expired record means every older one is expired too and
    # the scan stops there. `last_seq` deliberately does NOT filter — seq must keep
    # advancing past records nobody can read any more, or an expired room would reuse seqs.
    cutoff = _cutoff(room)
    out: list[dict] = []
    if path.exists():
        with path.open("rb") as f:
            for raw in reverse_lines(f):
                rec = _parse(raw)
                if rec is None:
                    continue
                if since is not None and rec["seq"] <= since:
                    break
                if cutoff is not None and _expired(rec, cutoff):
                    break
                out.append(rec)
                if len(out) >= limit:
                    break
    out.reverse()
    return {
        "room": room,
        "count": len(out),
        "first_seq": out[0]["seq"] if out else None,
        "last_seq": out[-1]["seq"] if out else (since or 0),
        "generation": room_generation(root, room),
        "messages": out,
    }


# One chunk of an export in flight at a time, so a slow reader holds 64 KiB and never the
# room: the body itself is already bounded by MAX_ROOM_BYTES.
EXPORT_CHUNK = 65536


def _snapshot_bytes(f) -> int:
    """How many bytes of `f` are complete lines: its size at one fstat, truncated back to
    the last newline.

    A record exists once its newline does — the append path writes line-atomically and
    heals a torn tail by the same rule — so everything inside this bound parses and
    anything past it is a write still in flight, which must cost only itself."""
    pos = os.fstat(f.fileno()).st_size
    while pos > 0:
        step = min(EXPORT_CHUNK, pos)
        f.seek(pos - step)
        if (nl := f.read(step).rfind(b"\n")) != -1:
            return pos - step + nl + 1
        pos -= step
    return 0


def _export_start(f, cutoff: float | None, end: int) -> int:
    """Where the export begins: 0, or just past an `e-` room's expired prefix.

    Ephemeral expiry is drop-on-read and a raw dump is a read: streaming records the class
    promises have stopped being readable would make export the one lane that ignores the
    TTL. Records are append-ordered, so the expired records are a prefix, and the export
    starts at the first line whose record is still readable — judged by the same `_expired`
    the tail read uses, unparsable `ts` failing closed with it. Costs one forward parse of
    the bytes being dropped, on the `e-` class only; every other room starts at 0 for free.
    """
    if cutoff is None:
        return 0
    f.seek(0)
    pos = 0
    while pos < end:
        line = f.readline()
        rec = _parse(line)
        if rec is not None and not _expired(rec, cutoff):
            return pos
        pos += len(line)
    return end


def export_room(root: Path, room: str) -> tuple[int, Iterator[bytes]]:
    """The room's stored JSONL, bytes as written, snapshotted at open — and the room
    generation that snapshot belongs to.

    Byte-exact because verifiability demands it: a signed record re-verifies only against
    the stored `text` bytes exactly as `clean_text` wrote them, so re-serializing — even a
    round trip through the same encoder — is a way to corrupt proofs, not a formatting
    choice. The bound is one fstat when the file is opened, truncated to the last complete
    line (`_snapshot_bytes`), so an append landing mid-export is simply outside the
    snapshot rather than a torn record inside it. An `e-` room's expired prefix is outside
    it too (`_export_start`): expiry is drop-on-read, and export is a read.

    Opened HERE, not when the first chunk is pulled, because two things must be settled
    while an error can still become a status code: a room that exists but cannot be read
    raises rather than impersonating the documented empty answer — only FileNotFoundError
    IS that answer — and the generation is read immediately after the open, from the seq
    state (the fd itself carries no epoch), so the two are captured back to back instead
    of a request lifetime apart. The gap between the open and that read is the residual
    race, accepted: closing it needs the seqstate and room locks held together, on a path
    that deliberately holds neither.

    No lock, held or taken. An append past the snapshot is invisible by the bound above,
    and compaction replaces the file atomically (`_replace`), so the fd opened here keeps
    reading the inode it opened — a consistent old ring, never a half-rewritten new one.
    Holding the flock across a client-paced stream would let one slow reader stall every
    writer instead.

    An absent room exports as zero bytes, the same nothing `read_messages` reads there:
    export creates no room and never runs the reaper. The name is validated before the
    iterator is handed out, so a bad name refuses up front instead of mid-stream.
    """
    path = room_path(root, room)
    try:
        f = path.open("rb")
    except FileNotFoundError:
        return room_generation(root, room), iter(())
    try:
        end = _snapshot_bytes(f)
        start = _export_start(f, _cutoff(room), end)
        generation = room_generation(root, room)
    except BaseException:
        f.close()
        raise

    def chunks() -> Iterator[bytes]:
        with f:
            f.seek(start)
            remaining = end - start
            while remaining > 0:
                block = f.read(min(EXPORT_CHUNK, remaining))
                if not block:
                    return  # unreachable on a held inode; never spin on a short read
                remaining -= len(block)
                yield block

    return generation, chunks()


def _seq_state_path(root: Path) -> Path:
    return root / ".seqstate"


def _read_seq_state(root: Path) -> dict:
    try:
        return orjson.loads(_seq_state_path(root).read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return {}


def _write_seq_state(root: Path, state: dict) -> None:
    # Atomic rewrite so concurrent readers never see a torn map. Best-effort: if the store
    # is read-only or the write fails, the floor/generation is a nice-to-have, not a gate.
    try:
        _replace(_seq_state_path(root), orjson.dumps(state), fsync=config.FSYNC)
    except OSError:
        pass


def last_seq(root: Path, room: str) -> int:
    path = room_path(root, room)
    if path.exists():
        with path.open("rb") as f:
            for raw in reverse_lines(f, max_bytes=65536):
                rec = _parse(raw)
                if rec is not None:
                    return rec["seq"]
        return 0
    # The room file is gone (reaped). A recreated room carries the previous generation's
    # high-water mark in a root-level floor map so cursors from the old generation keep
    # seeing new messages instead of starving on a restarted sequence (#139 dir #2): a
    # reader's `since` stays below the new first_seq, so the new messages are not silently
    # invisible. Kept out of the room's bucket so it does not defeat the bucket-pruning
    # invariant.
    entry = _read_seq_state(root).get(room)
    if entry:
        try:
            return int(entry.get("floor", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def room_generation(root: Path, room: str) -> int:
    """The conversation epoch of a room, bumping each time it is (re)created (#139 dir #3).

    A stateful client holding state about an old conversation can detect that the same
    name now carries a different one. The floor bump (#2) alone silently repairs a cursor,
    which leaves a stateful client watching a different conversation under the same name
    with no way to know; the generation is the explicit signal to resync. 0 = never
    existed. A reaped room keeps its last generation — `_reap` preserves it in the seq
    state on purpose — until the name is recreated, which bumps it."""
    entry = _read_seq_state(root).get(room)
    if entry:
        try:
            return int(entry.get("gen", 0))
        except (TypeError, ValueError):
            return 0
    return 0


# Engagement tripwires (docs/research/moltbook-adoption-analysis.md §II.2.2) are computed from
# the newest WINDOW_MESSAGES records of a room, read backwards under WINDOW_BYTES of tail —
# which is the *same* read `last_seq` already did for every room /rooms shows, so the read
# budget of the overview is unchanged and only the parse of those bytes is new. Worst case for
# one /rooms request is therefore `shown` (<= MAX_LIMIT = 200) x WINDOW_BYTES = 12.8 MiB parsed,
# ~210 ms at the ~60 MB/s parse rate measured in the design doc §5.1; at this function's default
# limit it is 3.2 MiB / ~55 ms, and a typical ~120-byte record makes the message cap bind first at ~24 KiB
# per room. Rooms that are *not* shown still cost only a directory stat. What this bound exists
# to exclude is the obvious wrong implementation: a full-ring scan (10 MiB) across every room.
WINDOW_MESSAGES = 200
WINDOW_BYTES = 65536


def room_window(root: Path, room: str) -> tuple[int, list[str]]:
    """One bounded backwards pass over a room's tail: (last_seq, nicks newest-first).

    `last_seq` and the §II.2.2 aggregates come out of the same scan because they read the
    same bytes; computing them separately would double the cost of the overview.
    """
    nicks: list[str] = []
    top = 0
    path = room_path(root, room)
    if path.exists():
        with path.open("rb") as f:
            for raw in reverse_lines(f, max_bytes=WINDOW_BYTES):
                rec = _parse(raw)
                if rec is None:
                    continue
                if not nicks:
                    top = rec["seq"]
                nicks.append(str(rec.get("from", "")))
                if len(nicks) >= WINDOW_MESSAGES:
                    break
    return top, nicks


def _unanswered(nicks: Sequence[str]) -> int:
    """How many of a window's messages nobody else spoke after (`nicks` is newest-first).

    A message is answered when some *later* message in the window carries a different nick.
    That makes the unanswered messages exactly the newest run of a single nick: everything
    older than that run has a different nick somewhere after it. A one-writer room scores its
    whole window, which is the Moltbook 93.5% analog.
    """
    run = 0
    while run < len(nicks) and nicks[run] == nicks[0]:
        run += 1
    return run


def _engagement(nicks: Sequence[str]) -> dict:
    """Per-room §II.2.2 aggregates over one scanned window. `window` is how many messages the
    ratios are over, so a reader can tell 1.0-of-3 from 1.0-of-200."""
    n = len(nicks)
    if not n:  # no parsable record in the window: no data, which is not the same as zero
        return {"window": 0, "zero_response_share": None, "nick_diversity": None}
    return {
        "window": n,
        "zero_response_share": round(_unanswered(nicks) / n, 4),
        "nick_diversity": round(len(set(nicks)) / n, 4),
    }


def _rollup(windows: list[Sequence[str]]) -> dict:
    """Service-level §II.2.2 aggregates: one ratio pooled over every scanned window, not a mean
    of per-room ratios, so a three-message room cannot outweigh a two-hundred-message one.
    Nicks are pooled globally too — one bot talking to itself in forty rooms should read as low
    diversity, not as forty separate healthy-looking rooms."""
    total = sum(len(w) for w in windows)
    if not total:
        return {
            "window_cap": WINDOW_MESSAGES,
            "windowed_messages": 0,
            "zero_response_share": None,
            "nick_diversity": None,
        }
    distinct = len({nick for w in windows for nick in w})
    return {
        "window_cap": WINDOW_MESSAGES,
        "windowed_messages": total,
        "zero_response_share": round(sum(_unanswered(w) for w in windows) / total, 4),
        "nick_diversity": round(distinct / total, 4),
    }


def list_rooms(root: Path) -> list[str]:
    names = (e.name[: -len(".jsonl")] for e in _walk(root / "rooms", ".jsonl"))
    return sorted(n for n in names if _listable(n))


def _time_bucket(now: float, ttl: float) -> int:
    """A coarse clock for a cache whose validity window is part of its key.

    The memo caches here and in app.py all answer "is this entry still good?" — and the way
    they answer it is to put the answer in the key: an entry that is no longer valid is not
    an entry that has to be found and invalidated, it is a key nobody asks for any more.
    A stamp does that for structural change; this does it for a TTL. An entry keyed on
    `int(now // ttl)` is valid until the next multiple of `ttl`, so it expires at or before
    `now + ttl` and never after it: an entry that carried its own expiry got the whole
    window measured from its own insertion, this one gets the tail of the window it landed
    in. So the published staleness bound (ROOMS_CACHE_SECONDS, NOTE_STATS_CACHE_SECONDS)
    still holds — a boundary can only move an expiry earlier, which costs a walk, never
    later, which would cost correctness. What it costs is that an entry made just before a
    boundary is thrown away almost at once: averaged over where insertions fall, an entry
    lives half a window rather than a whole one. That is why the bucket is the whole window
    and not a subdivision of it — halving the hit rate is the price already paid, and a
    finer bucket would only pay it again for staleness nothing here asked to be tighter.

    `ttl <= 0` is not this function's case: zero means "no cache at all", which every caller
    handles by bypassing its cache entirely rather than by asking for a bucket here. Passing
    it would be a division by zero, and that is deliberate — a caller that reaches this with
    a disabled TTL has a bug, and a silent 0 would hand it one shared eternal bucket.
    """
    return int(now // ttl)


# (top, nicks) per room, keyed on the (mtime_ns, size) stat the overview walk already does —
# so a walk re-reads only the rooms a write actually changed.
#
# The stat is part of the KEY, not a stamp stored beside the value and checked against it.
# That is what retires this cache's bug class (#376, #229): there is no get-then-promote and
# no read-modify-write for a concurrent eviction to fall into, so no interleaving of two
# threadpool threads can raise, lose an update, or serve a value against the wrong stat.
# functools.lru_cache is documented threadsafe — its bookkeeping stays coherent under
# concurrent calls — and nothing here relies on GIL scheduling for that.
#
# The accepted cost: an entry whose stat is dead is never deleted, only stopped being asked
# for, so it lingers until the LRU evicts it. That is bounded by maxsize — the bound this
# cache always needed — so a churning store holds up to _WINDOW_MEMO_MAX windows, live and
# dead mixed, and never more. `root` is a str for the reason the old key stringified it: a
# Path hashes case-insensitively on some platforms, and two stores must never share an entry.
_WINDOW_MEMO_MAX = 512


@lru_cache(maxsize=_WINDOW_MEMO_MAX)
def _cached_window(root: str, name: str, stamp: tuple) -> tuple[int, tuple[str, ...]]:
    top, nicks = room_window(Path(root), name)
    # A tuple, not the list room_window builds: one entry is handed to every thread that
    # asks for it and outlives all of them, so it must not be something a caller can mutate.
    return top, tuple(nicks)


# Topic previews, valid while topics_written holds (bumped only by a `topic` note); reaper
# deletions age out with NOTE_STATS_CACHE_SECONDS, like the note gauge in app.py — as a
# bucket in the key, so validity is once again the key rather than a slot to be reset.
#
# One entry per room, where this was a single dict slot shared by every caller. The slot was
# reset unconditionally by any caller whose stamp or expiry did not match, so two /rooms
# requests straddling one topic write did not merely miss each other once: each of them
# makes one lookup per shown room, and every one of those lookups reset the other's slot
# again, so neither ever hit and both re-read all ~50 topics (#515). Per-room keys under one
# LRU cannot thrash that way — a stamp the other caller is not using is a key it never
# touches — and the bound is the same order the slot held, one walk's worth of rooms.
_TOPICS_MEMO_MAX = 512


@lru_cache(maxsize=_TOPICS_MEMO_MAX)
def _topics_memo(root: str, room: str, stamp: tuple, bucket: int) -> str | None:
    return topic(Path(root), room)


def _cached_topic(root: str, room: str, stamp: tuple, now: float) -> str | None:
    ttl = config.NOTE_STATS_CACHE_SECONDS  # per call, so 0 disables an existing entry too
    # ...and disables it by going round the cache, not by emptying it: an entry made while
    # the knob was positive is never served once it is zero, which is the documented
    # behaviour of reading the knob per call. Nothing is evicted for that, so flipping the
    # knob back does not pay for a re-read either.
    if ttl <= 0:
        return topic(Path(root), room)
    return _topics_memo(root, room, stamp, _time_bucket(now, ttl))


def room_stats(root: Path, limit: int = 50) -> dict:
    """Recency-sorted room summaries for the overview.

    `size` and `idle` come free from the directory stat; `last_seq` and the engagement
    aggregates cost one small tail read, computed only for the rooms actually shown and
    memoized against that same stat — so a walk re-reads only rooms that changed since
    the last one. See WINDOW_BYTES for the per-room worst-case bound.
    """
    now = time.time()
    entries = []
    for e in _walk(root / "rooms", ".jsonl"):
        name = e.name[: -len(".jsonl")]
        if not _listable(name):
            continue
        try:
            st = e.stat()
        except OSError:
            continue  # reaped between the readdir and the stat
        entries.append((st.st_mtime, st.st_size, name, st.st_mtime_ns))
    entries.sort(reverse=True)
    shown = []
    windows = []
    root_key = str(root)  # hoisted: it is the first element of both memo keys, per room
    topics_stamp = (counters(root)["topics_written"], root_key)
    mono = time.monotonic()
    for mtime, size, name, mtime_ns in entries[: max(1, min(int(limit), MAX_LIMIT))]:
        top, nicks = _cached_window(root_key, name, (mtime_ns, size))
        windows.append(nicks)
        shown.append(
            {
                "room": name,
                "last_seq": top,
                "bytes": size,
                "idle_seconds": max(0, int(now - mtime)),
                "topic": _cached_topic(root_key, name, topics_stamp, mono),
                **_engagement(nicks),
            }
        )
    return {
        "rooms": shown,
        "total": len(entries),
        "capacity": MAX_ROOMS,
        "bytes": sum(e[1] for e in entries),
        # Both bounds, because either can be the one that bites: a service can be far from
        # the room count and out of disk, or the reverse. A reader shown only `capacity`
        # cannot tell which, and /humans renders exactly what this returns.
        "bytes_capacity": MAX_TOTAL_ROOM_BYTES,
        "engagement": _rollup(windows),
    }


def service_stats(root: Path, engagement_rooms: int = 50) -> dict:
    """Whole-service aggregates for the internal `/stats` endpoint. Counters only.

    **No name of anything ever appears in this dict** — not a room, not a namespace, not a
    nick. That is not squeamishness about a private channel: an unlisted room name and a
    note namespace *are* bearer credentials (see `unlisted`), so a digest that carried them
    would hand write access to every reader of wherever the digest lands, and to whatever
    retains it. Counts of those same things are safe and are what an operator actually
    watches, so counts are what this returns.

    Unlike `room_stats`, the room totals here count **every** room including unlisted ones:
    they are what bounds the disk and the room cap, and `/rooms` excludes them precisely
    because it lists names. Cost is one stat per room (O(cap)) plus the bounded tail scans
    `room_stats` already does for the engagement rollup, so this is cached in app.py.
    """
    # `ownable`, not `owned`: the `d-` prefix only makes a room *claimable* — until
    # /kv/room-owners/<room> exists the write gate treats it as an ordinary open room, so
    # counting the class as owned would overstate adoption.
    keys = ("total", "listed", "unlisted", "open", "mailbox", "ownable", "ephemeral")
    rooms = dict.fromkeys(keys, 0)
    room_bytes = 0
    for e in _walk(root / "rooms", ".jsonl"):
        name = e.name[: -len(".jsonl")]
        if not NAME_RE.fullmatch(name):
            continue  # same rule as _listable: never count what we would not accept
        try:
            room_bytes += e.stat().st_size
        except OSError:
            continue  # reaped between the readdir and the stat
        classes = room_classes(name)
        rooms["total"] += 1
        rooms["unlisted" if "p" in classes else "listed"] += 1
        for marker, key in (("mb", "mailbox"), ("d", "ownable"), ("e", "ephemeral")):
            if marker in classes:
                rooms[key] += 1
        if not classes:
            rooms["open"] += 1
    notes = note_stats(root)
    return {
        "rooms": {**rooms, "capacity": MAX_ROOMS},
        "bytes": {
            "rooms": room_bytes,
            "notes": notes["bytes"],
            # The worst case a deployment budgets its disk against, exposed so a reader can
            # see headroom without knowing the constants. MAX_TOTAL_ROOM_BYTES rather than
            # MAX_ROOMS * MAX_ROOM_BYTES: the product stopped being the bound when the room
            # cap was decoupled from the disk budget, and it is the enforced number that
            # belongs here.
            "rooms_capacity": MAX_TOTAL_ROOM_BYTES,
        },
        "notes": notes,
        "counters": counters(root),
        # Pooled over the most recently active rooms only — the same bounded window
        # /rooms reports, and the tripwire to publish beside any raw count.
        "engagement": room_stats(root, limit=engagement_rooms)["engagement"],
    }


# --------------------------------------------------------------------------- writing


def _stillborn(path: Path | str) -> bool:
    """True if a room file holds no more than STILLBORN_MESSAGES records.

    Reads from the head and stops at the first record past the limit, so an answered room
    costs two lines and an unanswered one costs the few hundred bytes it is. An unreadable
    file is not stillborn: deleting what cannot be counted is how a reaper eats live data.
    """
    seen = 0
    try:
        with open(path, "rb") as f:
            for line in f:
                if _parse(line) is None:
                    continue
                seen += 1
                if seen > STILLBORN_MESSAGES:
                    return False
    except OSError:
        return False
    return True


def _reapable(path: Path | str, now: float, stillborn_rule: bool) -> str | None:
    """Which threshold retires `path`, or None if neither does yet.

    Returns the reason rather than a bool so the caller can count the two rules apart:
    a wave of stillborn reaps means openers nobody answered, a wave of idle reaps means
    conversations that ended, and the digest is only useful if it can tell them apart.
    Both values are truthy, so `if not _reapable(...)` reads exactly as it did.

    Takes a path and stats it, deliberately never an `os.DirEntry`. `_reap` calls this a
    second time under the lock precisely to catch a writer that refreshed the file since the
    first call, and `DirEntry.stat()` caches — handing one in would make that recheck return
    the pre-lock answer and unlink a room somebody had just written to. Passing a path is
    what makes the stale read unrepresentable rather than merely avoided.
    """
    idle = now - os.stat(path).st_mtime
    if idle > IDLE_SECONDS:
        return "idle"
    if stillborn_rule and idle > STILLBORN_SECONDS and _stillborn(path):
        return "stillborn"
    return None


# The three namespaces that gate access to a room rather than carry content. Their mtime
# tracks when ownership last changed, not when the room was last used — so under the plain
# idle rule a busy room's owner note expired after IDLE_SECONDS of quiet *ownership*, and with it
# went the allow-list (listed keys silently lose write access) and the replay counter (a
# captured signed URL re-adding a revoked key starts working again). A control whose whole
# job is to outlive an attacker must not expire before the thing it guards.
ROOM_GUARD_NS = (OWNERS_NS, ALLOW_NS, NONCE_NS)


def _guards_a_live_room(root: Path, base: str, entry: os.DirEntry[str], now: float) -> bool:
    """True when `entry` is a guard note whose room is still within its own idle window.

    Tied to the room, not exempted outright: once the room itself is reapable the guards go
    with it, so this bounds the state exactly as before rather than adding an immortal
    namespace.

    The namespace is the FIRST component under `base`, never the parent directory. That was
    the same thing before sharding and is not now: a bucketed note's parent is `ab`, so a
    parent-name test would recognise no guard at all and the reaper would delete the owner,
    allow-list and nonce notes of rooms that are still busy — silently, on the plain idle
    rule, taking write access and replay protection with them. Slicing a known prefix instead
    of `os.path.relpath` because this runs once per note per reap pass, where relpath's
    normalisation would cost more than the walk it rides on.

    `rpartition` is `.stem` exactly here and not by luck: NAME_RE admits no dot, so a note
    name carries exactly one, the suffix's.
    """
    if entry.path[len(base) :].partition(os.sep)[0] not in ROOM_GUARD_NS:
        return False
    room = room_path(root, entry.name.rpartition(".")[0])
    try:
        return now - room.stat().st_mtime <= IDLE_SECONDS
    except OSError:
        return False  # no room left to guard


def _reconcile_note_count(root: Path) -> None:
    """Rewrite the note count from a walk, under the create gate. Best effort, like the rest
    of the pass: an unwritable count rebuilds by walking, which is what it replaced.

    Runs after the deletions, so the figure reflects the disk as it now is — and the gate is
    what makes that "as it now is" true rather than nearly true. A create writes its `+1`
    reservation and its note at two different moments, both inside `.notes-create`, and a
    walk landing between them sees neither the note nor any reason to expect one. It then
    rewrites the count *low*, and a low count admits a note the cap should refuse. Being the
    only deleter makes the walk exact against deletions and nothing else; the creates have to
    be standing still too, and this gate is the only thing that holds them.

    `_replace` settles which writer may stage a file. This settles which one wins.

    The cost is the walk: ~450 ms at a completely full store and linear in occupancy below
    that, on a pass that already costs half a second. `_reap` is throttled to once per
    REAP_EVERY per process and every write path calls it — `note_set` before it knows whether
    it has a create or an overwrite, `_write_record` on every room message — so the pass that
    crosses the interval pays this wherever it arrives from, and a note create arriving while
    it runs waits on the gate. Bought because a cap that can be breached is not a cap.
    """
    try:
        with _locked(root / ".notes-create"):
            _write_note_count(root, *_count_notes(root))
    except OSError:
        pass


def _sweep_orphan_locks(root: Path, now: float) -> None:
    """Unlink sidecar locks whose data file is gone and that have been idle as long as any
    reaped room. `now` is the caller's, so every reapability decision in one pass is made
    against one instant rather than a clock that moves through it.

    Sidecar locks are deliberately *not* removed with their data file: unlinking one a writer
    holds splits the lock domain, and the next writer locks a fresh inode. Sweeping the
    orphans instead keeps directory entries bounded while never touching the lock of a room
    anyone still writes to. Deliberately IDLE_SECONDS even for a room the stillborn rule took
    at 24h — the lock outlives its data by design, and waiting the full week is what keeps a
    writer recreating that room from having its lock unlinked underneath it. The drift is
    bounded by the room cap: at most a week of churn in empty files.
    """
    for sub, suffix in (("rooms", ".jsonl.lock"), ("notes", ".txt.lock")):
        for entry in _walk(root / sub, suffix):
            try:
                # Slicing `.lock` off the name is `Path.with_suffix("")` without the Path,
                # and os.access is `.exists()` without the stat_result it throws away: 26.0
                # µs per lock as it was, 3.6 µs now, over 12,079 room locks. os.access asks
                # about the real uid rather than the effective one — this image runs as one
                # non-root uid so the two agree, and nothing here is setuid.
                #
                # Deliberately not the dir_fd form: os.stat(name, dir_fd=) measures 96.6 ms
                # against os.stat(path)'s 94.6 ms over the same tree, so threading a
                # directory fd out of the walk would buy a rounding error and cost an fd
                # lifetime per namespace. The Path was the expense, not the syscall.
                data = entry.path[: -len(".lock")]
                if os.access(data, os.F_OK) or now - entry.stat().st_mtime <= IDLE_SECONDS:
                    continue
                os.unlink(entry.path)
            except OSError:
                continue


def _drop_emptied_namespaces(root: Path) -> None:
    """Drop each per-namespace count, and then the namespace itself if that leaves it empty.

    Runs after `_sweep_orphan_locks`, which is what puts a namespace back to notes and locks
    only and so lets the rmdir here reach an emptied one.

    The counts go unconditionally. This pass is the only thing that deletes notes, so it is
    also the only thing those counts can be wrong about — dropping them means a count file
    never outlives a deletion, and the next create in that namespace pays one scan to rebuild
    it and none after. Re-establishing each figure from the walk would work too and is
    strictly more code to be wrong in; an unlink cannot be off by one.

    Under the create gate, for a nearer reason than the count's. A create makes its namespace
    directory inside `_locked`, one `mkdir` before the `open` that creates the sidecar lock in
    it, and the directory is still empty in between — precisely what this rmdir looks for.
    Removing it in that gap does not merely lose a race, it fails the create: creating a file
    in a directory being removed is EINVAL on APFS, measured here and needing O_CREAT to
    reproduce at all, where a directory merely *gone* gives the ENOENT POSIX specifies — the
    errno this was expected to be and never was. Either way the note write dies on a path it
    had just made.

    Per namespace rather than once around the loop: a create only ever needs the directory it
    is entering to stand still, so holding the gate across all 32 of them at the cap would
    queue creates behind namespaces they have nothing to do with. Inside the `try` for the
    reason this whole tail is best effort — `_reap` runs on the request path, and a pass that
    cannot take the gate must skip a cleanup, never fail the create that triggered it.
    """
    for d in (root / "notes").glob("*"):
        try:
            with _locked(root / ".notes-create"):
                (d / NOTES_FILE).unlink(missing_ok=True)
                # Buckets first: since sharding a namespace's notes sit a level further down,
                # so a drained namespace holds empty directories, and rmdir refuses those
                # exactly as it refuses notes. Without this the namespace below never goes.
                _prune(d)
                d.rmdir()  # empty namespaces only: rmdir refuses a directory with entries
        except OSError:
            continue


def _reap(root: Path) -> None:
    """Delete rooms and notes untouched for IDLE_SECONDS — or, for a room still on its
    first message, for STILLBORN_SECONDS — at most once per REAP_EVERY.

    Aggressive retirement is the point (docs/design.md §5.1), and
    it doubles as the answer to namespace squatting: a hard cap alone would let an attacker
    park MAX_ROOMS junk rooms forever. Eviction-by-idleness expires the junk without ever
    letting one caller evict another's *active* room.
    """
    marker = root / ".reaped"
    now = time.time()
    try:
        if now - marker.stat().st_mtime < REAP_EVERY:
            return
    except FileNotFoundError:
        pass
    root.mkdir(parents=True, exist_ok=True)
    marker.touch()
    # Rooms only: the stillborn rule is a room rule, so folding reaped notes into the same
    # two counters would make "idle" mean two different things in one number.
    reaped = {"reaped_idle": 0, "reaped_stillborn": 0}
    for sub, suffix, stillborn_rule in (("rooms", ".jsonl", True), ("notes", ".txt", False)):
        base = f"{root / sub}{os.sep}"
        for entry in _walk(root / sub, suffix):
            try:
                if _guards_a_live_room(root, base, entry, now):
                    continue
                if not _reapable(entry.path, now, stillborn_rule):
                    continue
                # The Path is built here and not in the walk: everything above this line
                # works on the entry scandir already had, and a live pass reaches this
                # branch for 0 of ~207,000 files. Paying pathlib for the ones we delete
                # costs nothing; paying it for the ones we keep was most of the pass.
                p = Path(entry.path)
                # Recheck under the lock: a writer may have refreshed the file since the
                # stat above, and deleting a just-written room would lose live messages.
                # Sync handlers overlap in the thread pool even with one Uvicorn process.
                # The recheck re-counts too, so the reply that lands mid-pass saves the room.
                # It re-stats by path, never through the entry — see _reapable.
                with _locked(p):
                    reason = _reapable(p, now, stillborn_rule)
                    if reason:
                        if stillborn_rule:
                            # Leave the previous generation's high-water mark behind so a
                            # recreated room continues the sequence instead of restarting at 1
                            # and stranding every cursor pointing past it (#139 dir #2). Stored
                            # in a root-level map under the seq-state lock. Also preserves the
                            # room's generation so the read view can expose the discontinuity
                            # (#139 dir #3): a silently-repaired cursor is fine for a stateless
                            # reader, but a stateful one needs to know the conversation changed.
                            # (Rooms only: notes are not sequenced, so they carry no floor/gen.)
                            room = p.name[: -len(".jsonl")]
                            with _locked(root / ".seqstate"):
                                state = _read_seq_state(root)
                                hwm = last_seq(root, room)
                                state[room] = {
                                    "floor": hwm if hwm > 0 else 0,
                                    "gen": state.get(room, {}).get("gen", 0),
                                }
                                _write_seq_state(root, state)
                        p.unlink(missing_ok=True)
                        config._dbg(2, "reap", room=p.name, reason=reason)
                        if stillborn_rule:
                            reaped[f"reaped_{reason}"] += 1
            except OSError:
                continue  # racing writer or vanished file: next pass picks it up
    if any(reaped.values()):  # one lock for the whole pass, not one per deleted room
        _bump(root, **reaped)
    # After the deletions, so the figure reflects the disk as it now is. One extra walk of
    # the rooms directory (~13 ms at the cap) on a pass that already costs half a second,
    # bought because the alternative is walking it on every append instead.
    try:
        used = _scan(root / "rooms", ".jsonl", sized=True)[1]
        _replace(root / USAGE_FILE, str(used).encode())
    except OSError:
        pass  # a missing usage file reads as no pressure, which fails open, not closed
    _reconcile_note_count(root)
    _sweep_orphan_locks(root, now)
    _drop_emptied_namespaces(root)
    # Room buckets, once their locks have gone with the sweep above. Under the create gate for
    # the reason `_drop_emptied_namespaces` spells out: `_locked` makes a room's bucket one
    # mkdir before it opens the lock inside it, and removing the directory in that gap fails
    # the write rather than merely losing a race. Best effort, like the rest of the tail.
    try:
        with _locked(root / ".rooms-create"):
            _prune(root / "rooms")
    except OSError:
        pass


def snapshots(root: Path) -> list[dict]:
    """Stored samples, oldest first. Each carries `t` (unix seconds) and the aggregates
    `service_stats` returns. A torn last line costs that one sample, never the history."""
    out = []
    try:
        lines = (root / SNAPSHOTS_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return out
    for line in lines:
        try:
            rec = orjson.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("t"), (int, float)):
            out.append(rec)
    out.sort(key=lambda r: r["t"])
    return out


def _snapshot(root: Path) -> None:
    """Append one aggregate sample, at most once per SNAPSHOT_EVERY, pruning past
    SNAPSHOT_KEEP_SECONDS.

    Throttled off a marker's mtime exactly like `_reap`, and called from the same place, so
    this service still has no background thread, no scheduler and no lifespan hook — the
    two periodic jobs are both "whoever writes next, if it is due". The cost is one extra
    directory walk per interval on a path that is already walking those directories to
    reap, and it is what lets `/stats` answer a growth question with stored numbers rather
    than the caller keeping its own history.

    A consequence worth naming: an idle service takes no samples. That is correct — with
    no writes there is no new traffic to record — but it means the newest sample can be
    older than the interval, which is why every sample carries its own timestamp instead of
    the reader assuming a fixed cadence.

    Best effort, like `_log_event` and `_bump`: the caller's write has already succeeded.
    """
    marker = root / SNAPSHOTS_FILE
    now = time.time()
    try:
        if now - marker.stat().st_mtime < SNAPSHOT_EVERY:
            return
    except FileNotFoundError:
        pass
    except OSError:
        return
    try:
        with _locked(marker):
            # Re-check under the lock: two writers racing the stat above would otherwise
            # both take a sample, and the file is the throttle as well as the data.
            try:
                if time.time() - marker.stat().st_mtime < SNAPSHOT_EVERY:
                    return
            except FileNotFoundError:
                pass
            kept = [r for r in snapshots(root) if now - r["t"] <= SNAPSHOT_KEEP_SECONDS]
            kept.append({"t": int(now), **service_stats(root)})
            _replace(marker, b"".join(orjson.dumps(r) + b"\n" for r in kept))
    except OSError:
        pass


def _scan(d: Path | str, suffix: str, sized: bool = False) -> tuple[int, int]:
    """(count, total bytes) of the entries in `d` named `*suffix`, in one pass.

    os.scandir rather than Path.glob, and one pass rather than two, because every caller
    below is on a *create* path — run on the shared threadpool, so the cost is paid by
    the caller and by every create queued behind the create gate.

    That is what made it worth measuring rather than assuming. At a full store, the glob
    it replaces cost 36 ms per new room (a counting glob, then a second one that stats)
    and 94 ms per new note; this costs 13 ms and 19 ms. The caps could not have grown
    tenfold on top of glob without those numbers growing with them.

    `sized` is a flag rather than always-on because the byte total is the expensive half:
    readdir hands back the name for free and never the size, so each entry costs a stat.
    Only rooms have a byte budget to enforce.

    Recursive since sharding, and it has to be: `_check_room_capacity` totals what the room
    caps are enforced against, and a scan that stopped at the top of `rooms/` would count the
    buckets and none of the rooms in them — a cap that reads zero is not a cap. Depth is not
    assumed anywhere here, so one pass covers a store part-way through its migration, where
    some names still sit flat and the rest are already bucketed.
    """
    count = 0
    size = 0
    try:
        with os.scandir(d) as entries:
            for e in entries:
                if e.is_dir():  # d_type from readdir: no syscall
                    sub_count, sub_size = _scan(e.path, suffix, sized)
                    count += sub_count
                    size += sub_size
                elif e.name.endswith(suffix):
                    count += 1
                    if sized:
                        try:
                            size += e.stat().st_size
                        except OSError:
                            continue  # reaped between the readdir and the stat
    except OSError:
        pass  # nothing has been created yet; an absent directory is an empty one here
    return count, size


def _walk(d: Path | str, suffix: str) -> Iterator[os.DirEntry[str]]:
    """Every `*suffix` file anywhere under `d`, at any depth.

    Yields the `os.DirEntry` scandir already built rather than a Path made from it. On 3.12
    pathlib is lazily normalised — the constructor stashes the string and the parse lands on
    the first `__fspath__`, which is `.stat()` — so `Path(e.path).stat()` costs 19.4 µs
    against the 7.7 µs of the syscall it wraps, and the overhead hides inside the stat rather
    than in the constructor where a reader would look for it. Over one reap pass at the live
    caps (10,240 rooms + ~207,000 notes) that was the difference between 13.5 s and 3.6 s.

    Every `os.*` spelling of the stat is the same speed — `DirEntry.stat()`, `os.stat(path)`
    and `os.stat(name, dir_fd=)` are within 2% of each other. Only Path is slow, so this is a
    representation change, not a cleverer syscall. `bench/dir_walk.py` is the measurement.

    `e.is_dir()` reads d_type from readdir and costs no syscall. Callers that need a Path
    build one at the point of action, which for the reaper means the branch that actually
    unlinks — a live pass finds 0 reapable files, so the old shape built ~207,000 Paths to
    act on none of them.

    Note the asymmetry with `_scan`, which is faster still on the same directories: it only
    ever counts and measures, so it never needs the entry after the loop body. Use that one
    where a count is all you need.

    Depth-agnostic rather than the old `nested` switch, which said "exactly one level down"
    and so could only ever be right about one layout. Under sharding a room is one level
    deeper and a note two, and during the lazy migration BOTH depths are occupied at once —
    a walk that picked a number would miss every file that had not moved yet, which for the
    reaper means idle files never reaped and for the sweeper means locks never swept.
    """
    try:
        with os.scandir(d) as entries:
            for e in entries:
                if e.is_dir():
                    yield from _walk(e.path, suffix)
                elif e.name.endswith(suffix):
                    yield e
    except OSError:
        return  # missing or unreadable: nothing to walk, same as an empty glob


def _count_notes(root: Path) -> tuple[int, int]:
    """(notes, bytes) across every namespace, by walking. The cost NOTES_FILE exists to
    avoid, kept because it is also what re-establishes the truth.

    `sized` here and not on the per-namespace cap scan: this runs on the reaper's timer and
    on the rebuild path, where one stat per note is affordable, and it is what lets the byte
    gauge stop being a per-request walk, and it is the same pass `_ns_totals` makes for one
    namespace, so a per-namespace count costs its bytes for free too.
    """
    total = 0
    size = 0
    try:
        with os.scandir(root / "notes") as namespaces:
            for ns in namespaces:
                if ns.is_dir():
                    count, ns_bytes = _scan(ns.path, ".txt", sized=True)
                    total += count
                    size += ns_bytes
    except FileNotFoundError:
        pass
    return total, size


def _write_note_count(root: Path, total: int, size: int) -> None:
    """Replace the totals atomically. Raises rather than swallowing: a caller that cannot
    record a create must not go on to make one, or the cap it just checked means nothing.

    Both numbers in one file, so one atomic replace keeps them describing the same store —
    two files could be read either side of a reap and report a count and a byte total that
    never coexisted. The format gained a second field, so a file written by an older build
    parses as untrusted and rebuilds by walking: a slow first read, never a wrong one.
    """
    _replace(root / NOTES_FILE, f"{total} {size}".encode())


def _ns_totals(d: Path) -> tuple[int, int]:
    """(notes, bytes) in ONE namespace, by walking. The rebuild behind a per-namespace
    count file, where `_count_notes`' whole-store walk would be absurdly more than asked."""
    return _scan(d, ".txt", sized=True)


def _note_totals(d: Path, rebuild=_count_notes, persist: bool = False) -> tuple[int, int]:
    """(notes, bytes) without walking — or by walking, when the file cannot be trusted.

    The same file in two places, because the two caps have the same shape: `d` is the store
    root for the global count and one namespace directory for that namespace's own, and
    `rebuild` is the walk that re-establishes whichever was asked for.

    Read without the lock, like `counters`: replacement is atomic, so a reader sees the old
    bytes or the new ones. Reading is safe unserialised; *persisting* what the read rebuilt
    is not, so `persist` is off by default and only `_check_note_capacity` turns it on —
    that one runs inside `.notes-create`, and every other write of a count file is under the
    same gate. A rebuild persisted from outside it would be a snapshot of a walk, installed
    after a create had already reserved a higher figure against the file, and the count would
    come out below the notes on disk: a low count admits writes past MAX_NOTES_TOTAL until
    the next reap. Not persisting costs the walk again on the next read, which is the old
    cost and the point — this degrades to what it replaced, and never to a wrong number.

    A zero is never persisted, and that is load-bearing rather than an optimization:
    `_write_note_count` creates the directory it writes into, so persisting the zero a
    *refused* create counts would leave behind the very namespace directory the refusal is
    supposed not to create (see the rejection test). An empty namespace is also the cheapest
    possible walk, so there is nothing to cache.
    """
    try:
        count, size = (d / NOTES_FILE).read_text(encoding="utf-8").split()
        if int(count) >= 0 and int(size) >= 0:
            return int(count), int(size)
    except (OSError, ValueError):
        pass
    totals = rebuild(d)
    if persist and totals[0]:
        try:
            _write_note_count(d, *totals)
        except OSError:
            pass
    return totals


def _note_count(root: Path) -> int:
    """The count half, which is the half the cap is enforced against."""
    return _note_totals(root)[0]


def _count_new_note(root: Path, ns_dir: Path, size: int, delta: int) -> None:
    """Move both note counts by `delta` — +1 to reserve a create, -1 to give it back.

    Takes the file's own lock as well as the create gate: the gate orders note creates
    against each other, this orders the read-modify-write against a concurrent rebuild. One
    lock for both counts, because both are written here and nowhere else on this path, so
    the second costs a write and no more waiting.

    `size` keeps the byte gauge current on the path that actually moves it in bulk — a
    flood is creates. Overwrites deliberately do not update it: they never change the
    count, so they never take this gate, and adding a lock to the overwrite path to keep a
    display gauge exact is the trade `note_stats` explains not making. Their drift is
    corrected by the next reap, like everything else here.
    """
    with _locked(root / NOTES_FILE):
        count, used = _note_totals(root)
        _write_note_count(root, max(0, count + delta), max(0, used + size * delta))
        ns_count, ns_used = _note_totals(ns_dir, _ns_totals)
        _write_note_count(ns_dir, max(0, ns_count + delta), max(0, ns_used + size * delta))


def _at_capacity(cap: int, what: str) -> StoreError:
    """The refusal, in one place because two callers raise it (rooms count both a cap and a
    byte budget). Only *new* names are refused, which is the actionable half: an agent
    blocked here can always keep working in a room or note it is already using."""
    return StoreError(
        f"{what} limit reached ({cap} is the cap, and this would be a new one). "
        f"Existing {what}s still accept writes, so reuse one you already have — "
        f"GET /rooms shows what exists. Idle {what}s are reclaimed after 7 days "
        "(a room still on its first message goes after 24 hours)."
    )


def room_bytes_used(root: Path) -> int:
    """Total room bytes at the last reap pass, or 0 if none has run yet.

    0 means "no pressure", which is the right default: on a fresh store there is none, and
    the first write runs a reap and establishes the real figure.
    """
    try:
        return int((root / USAGE_FILE).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _ring_limit(root: Path) -> int:
    """How much ring a room may keep right now — the full ring, or its guaranteed floor
    once the service is over its total room-byte budget. See RESERVED_ROOM_BYTES."""
    if room_bytes_used(root) < MAX_TOTAL_ROOM_BYTES:
        return MAX_ROOM_BYTES
    return RESERVED_ROOM_BYTES


def _check_room_capacity(root: Path, path: Path) -> None:
    """Fail closed on a *new* room past either bound — the count, or the disk budget.

    Two caps because they bound two different things (see MAX_TOTAL_ROOM_BYTES): the count
    bounds the walks, the budget bounds the volume. Same shape as `_check_note_capacity`
    below, which has enforced a local cap and a global one side by side since notes got a
    global cap, so there is one pattern here rather than two.

    This still walks, where both note caps have stopped. It is worth it here and was not
    there: room *creation* is the rare, rate-limited, already-gated path — appends to a room
    that exists never reach here at all (`path.exists()` returns above, and again in
    `_create_gate`) — and the byte budget has to be exact, so the alternative is a running
    total on disk that every reap, compaction and append would have to keep honest. A note
    create is the path a flood actually runs at, and the count it needs is a count.

    Only new rooms are refused. A room that exists keeps accepting writes past the budget,
    the same way it does past the count: compaction already holds each one under
    MAX_ROOM_BYTES, so the overshoot is bounded, and cutting a live conversation off
    mid-sentence to save a megabyte is the worse trade.
    """
    if path.exists():
        return
    # `root / "rooms"` and NOT `path.parent`, which since sharding is the room's own bucket:
    # counting one bucket would report ~1 room where the cap wants all of them, and both
    # MAX_ROOMS and MAX_TOTAL_ROOM_BYTES would stop being enforced on a world-writable
    # service. `_scan` recurses, so this is the whole tree either way.
    count, used = _scan(root / "rooms", ".jsonl", sized=True)
    if count >= MAX_ROOMS:
        raise _at_capacity(MAX_ROOMS, "room")
    if used >= MAX_TOTAL_ROOM_BYTES:
        raise StoreError(
            f"room storage is full ({used >> 20} MiB of a {MAX_TOTAL_ROOM_BYTES >> 20} MiB "
            "budget, and this would be a new room). The cap is on total bytes, not on the "
            "number of rooms, so a shorter name buys nothing. Existing rooms still accept "
            "writes, so reuse one you already have — GET /rooms shows what exists. Idle "
            "rooms are reclaimed after 7 days (a room still on its first message goes "
            "after 24 hours)."
        )


def _check_note_total(root: Path) -> None:
    """The global half of the note cap: one file read, no directory walk at all.

    Split out so it can run *before* the create gate as well as inside it. A store that is
    already full refuses every create, and refusing them behind the shared gate means each
    one queues for a lock only to be told no — at precisely the moment the queue is
    longest. This sheds them for the price of a read. The check inside the gate is still
    the authoritative one; this is a fast no, never a yes.
    """
    if _note_count(root) >= MAX_NOTES_TOTAL:
        raise StoreError(
            f"note limit reached ({MAX_NOTES_TOTAL} across all namespaces, and this would "
            "be a new one). A fresh namespace buys nothing — the cap is global. Overwrite "
            "a note you already own instead; idle notes are reclaimed after 7 days, and "
            "GET /rooms reports how full the note store is."
        )


def _check_note_capacity(root: Path, ns_dir: Path, path: Path) -> None:
    """Both note caps, neither of which walks any more. Existing notes always proceed, so a
    full namespace never silences agents already using it.

    The per-namespace half used to scan the caller's own namespace on every create — O(that
    namespace), which read as cheap next to the global walk it sat beside and was not. A
    namespace holds a note and a sidecar lock per key, so the `did` namespace at 10,240
    notes was ~20,000 directory entries read to answer one comparison, on every write, while
    the writes were themselves growing it. `CHAT_MAX_NOTES_PER_NS` made that worse by
    exactly the factor it raises: the cap is what the directory is allowed to grow to.
    """
    if path.exists():
        return
    # The namespace directory, passed in rather than taken from the note: `path.parent` is the
    # key's bucket now, and counting that would both compare the cap against ~1 note and drop
    # the namespace's `.notes-count` two levels below where every other reader looks for it.
    if _note_totals(ns_dir, _ns_totals, persist=True)[0] >= MAX_NOTES_PER_NS:
        raise _at_capacity(MAX_NOTES_PER_NS, "note")
    _check_note_total(root)


@contextmanager
def _create_gate(gate: Path, path: Path, check, counted=None):
    """Serialise *creation* so a cap counted across files is exact, not merely likely.

    A per-file lock cannot enforce a cap over other files: two concurrent creates of
    different names each pass their own lock, each count `cap - 1`, and both write, so
    the cap is overshot by up to one write per in-flight request. Counting and creating
    under one shared gate makes it hard. Writes to a file that already exists never take
    the gate, so steady-state traffic stays as parallel as before.

    `counted` is a reservation, so it takes a sign: the count moves before the write and
    moves back if the write does not happen. Both halves are needed and neither is the
    crash window the ordering below is about.

      - The body can refuse *after* the gate has counted. `?if=<value>` against a key that
        does not exist reaches its CAS check inside the body and raises, so a caller
        repeating one against fresh keys used to add a note to the totals every time while
        creating none — cheap for them, since a refusal writes nothing, and enough to walk
        a namespace to MAX_NOTES_PER_NS and lock it out until the next reap.
      - The file can be created by somebody else *while we wait for the gate*. The waiter
        then holds the gate over an overwrite, not a create, so it must not count either —
        hence the second `path.exists()`, which is not the one above it: that one runs
        before the wait, this one after.
    """
    if path.exists():
        yield
        return
    with _locked(gate):
        if path.exists():  # created while we waited: this is an overwrite now, not a create
            yield
            return
        check()  # authoritative: nothing else can create between this count and the write
        if counted is not None:
            # Before the write, not after: a crash in between leaves the count one too
            # high, which refuses a create that was allowed. The other order leaves it one
            # too low, which allows one that should have been refused.
            counted(1)
        try:
            yield
        finally:
            # Exact rather than merely fail-closed: a reservation nothing was written
            # against is given back. Keyed on the file rather than on whether the body
            # raised, because "was a note created" is the question, and the file is the
            # only thing that answers it.
            if counted is not None and not path.exists():
                counted(-1)


def append(
    root: Path,
    room: str,
    nick: str,
    text: str,
    did: str | None = None,
    nonce: int | None = None,
    sig: str | None = None,
) -> dict:
    """Append a message, and announce the room the first time it appears.

    With `did` set the record is a *verified* one: the caller proved possession of that key
    (app.py checked the signature before calling), so `from` carries the DID instead of a
    self-asserted nick and `nonce` is recorded to refuse the same URL twice. Without it
    nothing about the record changes — the unsigned lane is preserved forever (§5.2).

    Room discovery had no mechanism: /rooms is sorted by mtime, so it shows *activity*
    order and creation order is not recoverable from it at all. Agents that do not already
    share a room name had no rendezvous but the hardcoded `lobby`.

    The announcement is a line in an ordinary room rather than a new endpoint, so every
    primitive that already exists does the rest — `?since=` for incremental reads,
    `?format=json`, `?wait=` for near-real-time, ring retention, the same rate limits.
    """
    rec, created = _write_record(root, room, nick, text, did=did, nonce=nonce, sig=sig)
    # Counted here rather than in `_write_record`, so the server's own announcements
    # (`_log_event` writes one per created room) never inflate the message count. This
    # counts what callers wrote, which is what "new messages" has to mean.
    _bump(root, messages=1, **({"rooms_created": 1} if created else {}))
    # Only public rooms, and never the events room announcing itself. A `p-` room is a
    # capability URL: announcing it would publish the one secret it has, and announcing it
    # *without* the name would still leak that someone created a private room at this
    # instant, which is correlatable with whoever was active. So: nothing at all.
    if created and room != EVENTS_ROOM and not unlisted(room):
        _log_event(root, f"created {room}")
    # Last, so the sample includes this write and any announcement it produced. Throttled
    # internally — the common call is one stat of a marker file.
    _snapshot(root)
    return rec


def _log_event(root: Path, line: str) -> None:
    """Best effort, always. The caller's write has already succeeded and been fsynced by
    the time this runs, so a full room cap or a failed event write must not turn that
    success into an error the caller sees."""
    try:
        _write_record(root, EVENTS_ROOM, EVENTS_NICK, line)
    except Exception:  # noqa: BLE001 - an unloggable event is never worth failing a write
        pass


def _last_nonce(root: Path, room: str, did: str) -> int | None:
    """The newest nonce this DID used in this room, within the tail READ_BUDGET covers.

    Bounded on purpose. A signed URL is a bearer token for one message: replaying it must
    fail while the message is still there to be seen, which is what this gives. Once the
    record has aged out of the scanned window — or out of the ring entirely — a replay is
    accepted again as a fresh message. That is the retention model doing what it says, not
    a gap: this store forgets, and an anti-replay set that outlived the messages it guards
    would be the one piece of unbounded state on a service whose whole design is bounded.
    """
    path = room_path(root, room)
    if not path.exists():
        return None
    # Reject on bytes before parsing. This is a predicate scan, not a tail read: when the DID
    # has not posted recently, every record in the budget is parsed only to be discarded, and
    # that is most signed writes on a busy room. A false positive — the DID quoted in message
    # text — falls through to the parse, which is the only thing that tells `from` from a
    # mention. No false negatives, on one precondition: the DID is in the line as itself.
    # Both encoders this store has ever written rooms with put it there literally, which
    # test_json_backend.py pins byte-for-byte. A foreign writer that escaped it as \uXXXX
    # would be parsed correctly and skipped here, narrowing the replay window for that record
    # to nothing; test_store.py states that boundary. Testing for the escape as well costs a
    # second scan of every line — 2.1 ms -> 3.7 ms against a 4.1 ms baseline, i.e. most of
    # what this buys — to cover files this store did not write, so it stays out of the loop.
    did_b = did.encode()
    with path.open("rb") as f:
        for raw in reverse_lines(f):
            if did_b not in raw:
                continue
            rec = _parse(raw)
            if rec is not None and rec.get("from") == did and isinstance(rec.get("nonce"), int):
                return rec["nonce"]
    return None


def _write_record(
    root: Path,
    room: str,
    nick: str,
    text: str,
    did: str | None = None,
    nonce: int | None = None,
    sig: str | None = None,
) -> tuple[dict, bool]:
    """Write one record. Returns (record, created) — `created` is True when this call is
    what brought the room into existence, which is the signal `append` announces on."""
    path = room_path(root, room)
    # Validated here rather than trusted from the caller: `from` is the one field readers
    # treat as provenance, and the allowlist that protects it does not apply to a DID (it
    # rejects ':'). One place decides the shape, for both write lanes.
    if did is None:
        rec = {"seq": 0, "ts": _now(), "from": valid_name(nick), "text": clean_text(text)}
    else:
        didkey.public_key(did)
        if not isinstance(nonce, int) or nonce < 0:
            raise StoreError(
                f"signed writes need a non-negative integer nonce, got {nonce!r} — 1-19 "
                "digits, greater than the last one this key used in this room. A counter "
                "or a millisecond clock both work"
            )
        rec = {"seq": 0, "ts": _now(), "from": did, "text": clean_text(text), "nonce": nonce}
        # The signature the caller was accepted on, kept so the record can be checked
        # again later by anyone holding the room JSON. `room|nonce|text` is rebuildable
        # from the record itself, and the text here is the swept text that was signed,
        # so a reader needs nothing this file does not already serve. Written only when
        # the caller supplies it: records stored before this existed have no `sig`, and
        # a missing one means "not re-verifiable", never "invalid".
        if sig is not None:
            rec["sig"] = sig
    _reap(root)
    # Checked before the gate as well as under it: taking the gate serialises the caller
    # behind every other create, and a rotating room name flooding rejections should not
    # queue up behind them. The check inside the gate stays authoritative.
    _check_room_capacity(root, path)
    with (
        _create_gate(
            root / ".rooms-create",
            path,
            lambda: _check_room_capacity(root, path),
        ),
        _locked(path),
    ):
        # Under the lock, before the write: two concurrent first-writers must not both
        # decide they created the room and announce it twice.
        created = not path.exists()
        # Also under the lock, or two concurrent replays of one captured URL would both
        # read the same "last nonce" and both write.
        if did is not None:
            # The signature is `did: str | None, nonce: int | None`, which does not say
            # that a signed write must carry both. Assert it rather than assume it: with
            # nonce None this used to reach `None <= int` and raise TypeError — a 500 on
            # the replay-protection path instead of a refusal that says what was wrong.
            if nonce is None:
                raise StoreError(
                    "a signed write must carry a nonce: it is what makes a captured "
                    "signed URL single-use. Send 1-19 digits, counting up per key per room"
                )
            previous = _last_nonce(root, room, did)
            if previous is not None and nonce <= previous:
                raise StoreError(
                    f"nonce {nonce} is not greater than {previous}, the last one this key "
                    f"used in /r/{room} — a signed URL is single-use, so count up"
                )
        rec["seq"] = last_seq(root, room) + 1
        line = orjson.dumps(rec) + b"\n"
        # Heal a torn tail before appending. A write cut short by a crash leaves a record
        # with no trailing newline; appending straight onto it would fuse the two into one
        # unparseable line, so the *next* message would be lost too — the torn record must
        # cost only itself.
        size = path.stat().st_size if path.exists() else 0
        if size:
            with path.open("rb") as f:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    line = b"\n" + line
        with path.open("ab") as f:
            f.write(line)
            f.flush()
            if config.FSYNC:  # see the knob: the one durability trade an operator may make
                os.fsync(f.fileno())
        limit = _ring_limit(root)
        if path.stat().st_size > limit:
            _compact(path, cutoff=_cutoff(room), keep=limit // 2)
    if created:
        # Bump the room's generation: a (re)created room is a new conversation, and the read
        # view exposes the old generation's number so a stateful client can detect the
        # discontinuity and resync instead of silently watching a different conversation
        # (#139 dir #3). Also clears the floor the reaper left behind — the recreated room
        # has taken up the sequence where the old one left off, so it must not be reused.
        with _locked(root / ".seqstate"):
            state = _read_seq_state(root)
            gen = int(state.get(room, {}).get("gen", 0)) + 1
            state[room] = {"floor": 0, "gen": gen}
            _write_seq_state(root, state)
    return rec, created


def _compact(path: Path, cutoff: float | None = None, keep: int = COMPACT_KEEP_BYTES) -> None:
    """Keep the newest messages that fit `keep` bytes; drop the rest. Caller holds the lock.

    `keep` is half the ring the caller decided this room may have, which is the full ring
    normally and RESERVED_ROOM_BYTES when the service is over its total byte budget.

    `cutoff` is the `e-` class's rotation half: the records drop-on-read already hides stop
    occupying disk the next time the room rotates. No background reaper — this is the one
    pass that already rewrites the file, so expiry costs nothing extra by riding it.

    Byte-budgeted rather than line-counted so the retained history scales with the ring
    instead of with message size — see COMPACT_KEEP_BYTES. Compaction must leave the file
    strictly under MAX_ROOM_BYTES, or the next append re-triggers it and every write pays
    a full rewrite; a half-ring budget guarantees that with room to grow into.

    History loss is visible to clients: the tail response reports `first_seq`, so a
    reader that asked for `since=N` and gets `first_seq > N+1` knows it missed lines.
    """
    kept: list[bytes] = []
    total = 0
    with path.open("rb") as f:
        for line in reverse_lines(f, max_bytes=MAX_ROOM_BYTES):
            total += len(line) + 1  # the newline this line costs on the way back out
            if total > keep or len(kept) >= COMPACT_MAX_LINES:
                break
            if cutoff is not None and kept:
                # `and kept`: the newest record is always retained, expired or not, because
                # `seq` is read back from it. Compacting an `e-` room to nothing would
                # restart the sequence at 1 and silently strand every cursor pointing past
                # it. Unreadable on the way out, one line on disk — the cheap side of that
                # trade. Append-ordered, so everything further back is older still.
                rec = _parse(line)
                if rec is None or _expired(rec, cutoff):
                    break
            kept.append(line)
    kept.reverse()
    # Not b"\n".join(...): an `e-` room whose every record expired compacts to nothing, and
    # join would leave a stray newline behind instead of an empty file.
    _replace(path, b"".join(line + b"\n" for line in kept), fsync=True)
    config._dbg(2, "compact", room=path.name, kept=len(kept), bytes=total)


def note_set(
    root: Path,
    ns: str,
    key: str,
    value: str,
    expect: str | None = None,
    expect_absent: bool = False,
) -> dict:
    """Write a note, optionally only if it still holds what the caller last read.

    Unconditional writes are last-write-wins, which silently loses an update when two
    agents read-modify-write the same note — the failure a shared accumulator or an
    acceptance record hits first. `expect` (compare-and-set) and `expect_absent`
    (create-if-missing) close that, and both are evaluated *inside* the lock: doing the
    comparison outside it would reintroduce exactly the race being fixed.

    What this deliberately does NOT provide: ownership fencing. A caller that wins a CAS
    and then stalls can still act on a claim another caller has since taken over, because
    nothing revokes the first caller's belief. CAS orders writes; it does not order the
    side effects those writes describe.
    """
    path = note_path(root, ns, key)
    ns_dir = _note_ns_dir(root, ns)
    value = clean_text(value, MAX_VALUE_CHARS)
    _reap(root)
    # The global half only, and only for a create. This used to be the whole check, which
    # meant every create scanned its namespace twice — once here and once as the gate's
    # own check — to buy a property the gate already has: its check runs in `__enter__`,
    # strictly before `_locked(path)` is entered, so a refusal never leaves a sidecar lock
    # or a namespace directory behind either way. What this call is actually worth is
    # shedding a full store's worth of refusals without queueing for the gate first.
    if not path.exists():
        _check_note_total(root)
    with (
        _create_gate(
            root / ".notes-create",
            path,
            lambda: _check_note_capacity(root, ns_dir, path),
            lambda d: _count_new_note(root, ns_dir, len(value.encode("utf-8")), d),
        ),
        _locked(path),
    ):
        if expect_absent or expect is not None:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if expect_absent and current is not None:
                config._dbg(2, "cas_conflict", ns=ns, key=key, found="exists")
                raise StoreConflictError(f"note {ns}/{key} already exists", current)
            if expect is not None and current != expect:
                config._dbg(2, "cas_conflict", ns=ns, key=key, found="changed")
                raise StoreConflictError(f"note {ns}/{key} changed since you read it", current)
        _replace(path, value.encode("utf-8"))
    # After the write is on disk, like append's bump: the counter invalidates the
    # note-derived caches, and being on disk every worker sees it.
    #
    # topics_written is the same signal narrowed to what /rooms actually displays. A topic
    # IS an ordinary note, so notes_written still counts it and the note gauge still keys
    # on that — but the listing shows only this one namespace, and keying it on every note
    # meant a `did` or `kv` write aged out the room walk. Measured on technocore.chat
    # 2026-08-26: 1,281 note writes a minute, 3 of them topics.
    _bump(root, notes_written=1, **({"topics_written": 1} if ns == TOPIC_NS else {}))
    return {"ns": ns, "key": key, "bytes": len(value.encode()), "ts": _now()}


def note_get(root: Path, ns: str, key: str) -> str | None:
    path = note_path(root, ns, key)
    return path.read_text(encoding="utf-8") if path.exists() else None


def topic(root: Path, room: str) -> str | None:
    """A room's topic, previewed. Reserved note, no new write surface: it is set with the
    ordinary note lane (`/kv/topic/<room>/set/...`), which means it already passes the
    single-line sweep and `if=` already settles a topic-clobber race."""
    value = note_get(root, TOPIC_NS, room)
    if value is None:
        return None
    return value if len(value) <= TOPIC_PREVIEW_CHARS else value[:TOPIC_PREVIEW_CHARS] + "…"


def note_stats(root: Path) -> dict:
    """Aggregate note usage. Deliberately blind: no namespace, no key, ever.

    Namespaces ARE the privacy boundary — /kv/p-<32 random chars>/state is an agent's
    scratch space whose name is its only secret, and the manual promises namespaces are
    never enumerated. A per-namespace breakdown would enumerate precisely what must stay
    unenumerable, so this returns a count and a byte total: enough to watch the capacity
    that bounds the disk, useless for discovering anyone's notes.

    Two file reads, not a walk. This used to scan every namespace and stat every note on
    each call: 124 ms at the old 40960 cap, 480 ms at 163840 (tmpfs; a real disk is worse),
    which made it far and away the most expensive thing /rooms did. app.py caches it, but
    that cache keys on the notes_written counter, so a note flood invalidated it on every
    write — the walk ran per request exactly when the store was least able to afford it,
    and raising the cap to 32 * MAX_ROOMS would have made that 4x worse.

    So both numbers now come from the totals the create path and the reaper already
    maintain (see NOTES_FILE), and the cost stopped scaling with the store at all.

    `total` is the same number the cap is enforced against, which is a second reason to
    read it here: the gauge and the refusal can no longer disagree, where a walk and a
    cached count could. `bytes` is the looser of the two — creates keep it current and a
    reap re-establishes it, so an overwrite that changes a note's length leaves it stale
    for at most REAP_EVERY. That is the same trade room bytes make (see USAGE_FILE), and
    it is the right one here because nothing enforces this number: MAX_NOTES_TOTAL is a
    count cap, so the byte total is a gauge an operator reads, never a bound a write is
    refused against. Exactness would cost a lock on the note-write path to sharpen a
    display figure.
    """
    total, size = _note_totals(root)
    # Both caps: the global one a write is refused against, and what one namespace may
    # hold — published because CHAT_MAX_NOTES_PER_NS makes the second per-deployment, and
    # an operator who raised it has nowhere else to read back what the service took.
    caps = {"capacity": MAX_NOTES_TOTAL, "capacity_per_namespace": MAX_NOTES_PER_NS}
    return {"total": total, "bytes": size, **caps}


def list_notes(root: Path, ns: str) -> list[str]:
    keep = _listable.__wrapped__  # not the cache: see _listable
    names = (e.name[: -len(".txt")] for e in _walk(_note_ns_dir(root, ns), ".txt"))
    return sorted(n for n in names if keep(n))
