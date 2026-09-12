"""The abuse budget: who is calling, what they may spend, and what a refusal says.

Moved out of app.py whole. The knobs arrive as PARAMETERS, not module reads: app keeps
the module-level aliases (RATE_READ, CLIENT_IP_HEADER, MAX_BUCKETS, ...) that both the
tests' monkeypatch.setattr(app, ...) and config.override() re-bind, and passes them into
take()/refund()/client_ip() on every call — a config read here would bypass both mutation
paths and silently break them. The mutable state (_buckets, _identities, _requests, the
waiter slots, the proxy evidence) lives here and is re-exported by app as the SAME
objects, so app._buckets.clear() and friends keep clearing what the limiter reads.

The core is a token bucket per (client IP, kind). `burst` is the bucket's capacity, and
defaults to one minute's worth because that is what a per-minute budget means. A budget
measured over a *day* needs the two apart: the capacity is the whole day's allowance and
`per_min` is only the rate that hands it back. Folded together, a 20-rooms-per-day budget
would be a bucket holding 0.0139 tokens, which never reaches the 1.0 a grant costs — the
limit would refuse everything.
"""

import hashlib
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager

from starlette.requests import Request
from starlette.responses import Response

import config
import store

# Headers a CDN sets and overwrites on every request. Their *presence* is not permission to
# trust them — a direct caller can send any of them, which is the whole reason
# CLIENT_IP_HEADER is opt-in — but it does mean the request plausibly arrived through that
# CDN, and if we are not configured to read one, every caller behind it shares a single
# rate-limit identity. That failure is silent and it gets worse the longer the budget: a
# shared per-minute limit merely feels strict, a shared per-DAY room budget is a global
# lockout nobody can distinguish from "the service is broken". So the mismatch is counted
# and published in /stats rather than guessed at. Detection, not trust.
PROXY_IP_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "true-client-ip")

# The paths that cost nothing, named once because the 429 body and the manual both list
# them. A 429 that points at a path which is itself rate limited is advice that fails at
# exactly the moment it is taken.
# The paths a throttled agent is told it may still reach. /healthz is NOT here, and its
# absence is the point: it is genuinely never rate limited — the handler simply never calls
# take() — but naming it in a 429 is handing a throttled caller a free endpoint at the exact
# moment it is looking for one. Measured 2026-09-02: /healthz was 10.4% of all traffic
# (19.6 req/s, 2,478 of 2,480 requests arriving through the tunnel rather than from the
# container's own probes) while appearing in no other document. This list, rendered into the
# manual as __FREE_PATHS__ and into every 429 body, was the only place the service mentioned
# it in prose. The list stays honest either way: it promises the named paths are free, never
# that they are the only free ones.
#
# The api-catalog still advertises /healthz as the service's `status` link, and /openapi.json
# still describes the operation. Those are deliberate, machine-readable and asked for; a
# rate-limit refusal is neither.
FREE_PATHS = "/, /llms.txt, /skill.md, /patterns.md, /interop.md, /auth.md, /openapi.json, /config and /.well-known/*"

# Bounded LRU, because every unseen IP would otherwise add entries forever and the
# proxy's per-IP rule caps requests per IP, not the number of distinct IPs — a rotating
# IPv6 /64 or a distributed flood would grow this until the 128 MiB container OOMs.
# Eviction costs nothing at the margin: an entry idle for a full refill window has
# refilled to `per_min`, so forgetting it is identical to keeping it, and LRU order
# evicts the idlest first. A flood of >MAX_BUCKETS *concurrently active* IPs does lose
# limiter state — which is why the authoritative limit belongs in the proxy (see README).
MAX_BUCKETS = 20_000
_buckets: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()

# Request counters for /stats. Deliberately in-process (the store's counters are the
# durable ones): traffic is only ever read as a rate, and a rate needs the uptime that
# sits beside it, not a number that outlives the process it describes.
_requests = {"read": 0, "write": 0, "rate_limited": 0, "duplicate": 0, "followed": 0}
# Two numbers that together say whether per-IP limits are actually per-IP. `proxied` counts
# requests that carried a CDN header we are not configured to read; `identities` is how many
# distinct client IPs the limiter has ever keyed on. A busy service showing a high `proxied`
# and an `identities` of 1 is not rate limiting anyone individually — it is rate limiting
# the CDN, and the room budget is being shared by the entire internet.
_proxy_evidence: dict[str, int] = {"proxied_requests": 0}
_identities: set[str] = set()
MAX_IDENTITIES = 50_000  # bounded like _buckets; a counter that OOMs is not a diagnostic

# The cross-sender duplicate ring: the write-path abuse filter. Keyed per (room, digest
# of the NORMALISED text) with no sender anywhere in the key, because the flood it
# exists for is one canned sentence from thousands of identities — a per-caller key
# never sees it (measured on production: 24% of duplicate messages were same-sender,
# 76% were not).
#
# This is the successor to, and replacement for, the per-caller retry map that used to
# sit here: that one answered a repeat with the message it repeated, which was a retry
# helper and never an abuse filter, shipped off by default and was never activated —
# while a duplicate write costs the per-room flock the whole write path serialises on.
# Same key shape as a retry map minus the caller, same bounds discipline.
#
# ONE map with a global cap, not a map per room: a per-room cap bounds each room's ring
# but leaves the ring-count unbounded, and MAX_ROOMS rooms x 4096 keys is gigabytes
# against a 128 MiB container. The global cap is what an attacker posting one long text
# to every room can grow it to, nothing more. Evicting early costs a duplicate, which is
# the cheaper failure — the same sentence this ring already accepts under --workers N.
#
# Only ACCEPTED writes are recorded. A refused copy adds no timestamp, so a farm cannot
# drag its own window open by hammering: the phrase becomes acceptable again exactly
# `window` after the last copy that landed, never later.
#
# Guarded by a lock, unlike every other structure in this module. The waiter counters are
# safe unlocked because they are only ever touched by the single-threaded event loop, and
# the buckets are read-modify-write on ONE key so a lost update costs a fraction of a
# token. This one is neither: both write lanes reach it from a threadpool (the GETs are
# sync endpoints, the POST goes through run_in_threadpool), and the sweep walks and
# deletes from the front while another thread may be inserting — which is an
# `OrderedDict mutated during iteration` RuntimeError, or a KeyError on a key the other
# thread just evicted, i.e. a 500 on exactly the write path the filter exists to protect.
# It is a leaf lock, and what it is held for is bounded by construction: two tuple
# rebuilds (the live timestamps, and the room's ring), one scan of at most DUPE_RING
# digests for the share count, and at most a dozen dict operations. Never across the
# flock, the append or any I/O, so nothing can deadlock against it. Measured here over
# 100k accepted writes against a full ring and a full key map, one critical section:
# ~1.8us before the share cap, ~4.6us after — 2.6x, and the growth is the share scan and
# the ring rebuild, both C-level over 64 items. Still bounded by DUPE_RING and
# max_copies rather than by anything a caller controls, and the hash that precedes the
# lock costs more than the section itself.
MAX_DUPE_KEYS = 4096
_dupes: OrderedDict[tuple[str, bytes], tuple[float, ...]] = OrderedDict()
_dupes_lock = threading.Lock()

# The share cap, and the reason there are two signals rather than one: a window is a RATE,
# so a repeater beats it by posting a second outside it. Measured (issue #697, room
# mb-jinken, /export): 67 byte-identical copies of one 83-character sentence from one
# signed key over 10h, median gap 137s against a 120s window — only 21 of the 67 gaps were
# inside it, so the filter almost never fired, and the copies still ended as 58% of the
# room. `mb-` rooms cannot be owned, so no allow-list, mute or delete exists to fall back
# on: the window is the only remedy there is, and this is the shape it cannot see.
#
# So a second signal that is deliberately not a time: per room, the DIGESTS of the last
# DUPE_RING messages that reached the filter at all, and a refusal once one digest would
# hold more than DUPE_SHARE of the ring's SLOTS. Share of capacity, not of the entries
# present, so the cap bites at the same 32 copies in a room whose ring is a quarter full
# as in one that is full — a repeater alone in a quiet room is exactly the case, and a
# rule that waited for 64 accepted messages before it could speak would let that repeater
# land 64. What it measures is composition, not rate: 137s apart, 4235s apart or once a
# week all meet the same bar, because the bar is what the room READS like.
#
# A (reserved-at instant, digest) pair, so each ring slot carries the same identity the
# window's timestamp already gives a _dupes entry. Nothing here reads that time — expiry is
# the window's job and this rule has none — it exists ONLY so dupe_release matches the slot
# its OWN reservation added, never a newer one. Bare digests could not do that: every copy
# of a digest is interchangeable, so a stale release (the write it reserved for failed long
# after a later copy of the same text landed and stuck) would `list.remove` that later,
# successful slot and drop the room's live count of the phrase to zero. The instant breaks
# the tie the digest cannot. An earlier draft stored the same pair but *filtered* on it
# (dropping every equal entry), which wiped both slots when two reservations shared one
# time.monotonic() float — the release below removes exactly one with `list.remove`, so a
# collision costs one slot and the other copy survives, which is the whole point.
#
# Rejected: reading the room's own messages for a true denominator — that is a store read
# inside the lock below, the one thing its critical section promises never to do.
# Rejected: widening the window, which config.py's own sizing already rules out (the ring
# hits MAX_DUPE_KEYS around 300s, so memory becomes the limiter, not the window) and which
# any repeater beats by adding a second to its sleep. Rejected: a per-sender cap — the key
# has no sender in it on purpose, 76% of measured duplicates come from a different sender
# than the copy before them.
#
# Bounded exactly as _dupes is and for the same reason: at most MAX_RING_ROOMS rooms of at
# most DUPE_RING digests, LRU-evicted, so a caller posting to every room in existence can
# grow this to a fixed 32k digests (512 KiB) and no further. Only messages the filter could
# refuse enter it — a text under DUPE_MIN_LENGTH never reaches the ring — so a room full of
# "ok" neither dilutes a repeater's share nor earns one of its own. Guarded by _dupes_lock
# rather than a second mutex: it is read and written in the same critical section as
# _dupes, on the same call, and two leaf locks taken together are one lock with more ways
# to get it wrong. Not an operator knob, for the same reason MAX_DUPE_KEYS and MAX_BUCKETS
# are not: it is a bound, and the window's 0 remains the one opt-out — window <= 0
# short-circuits before the key is built, so it turns this off with everything else.
#
# Per worker, like every other structure here, and it shards the same way the window does
# (config.py sizes that loss for DUPE_FILTER_SECONDS): under --workers N a room's messages
# are spread over N rings, each capping its own stream at DUPE_SHARE, so the share one text
# can hold of what readers actually see is up to N x 32 of the last N x 64 — the ratio
# survives an even split and only an uneven one moves it. The authoritative fix for that is
# the same as for the rate limit: one worker, or a shared store in front of it.
#
# Keyed by (room, GENERATION), not by bare name, which is the one thing the window above does
# not need and this does. The window's entries expire on their own — a stale timestamp is
# swept `window` seconds after it lands — but a ring slot has no clock (the share cap is
# composition, not rate; the block above says so), so a bare-name ring outlives the room it
# describes forever. store._reap deletes a room's file and preserves only its seq floor and
# generation; it never imports limit and cannot clear this map. So a room reaped and later
# recreated under the SAME name would inherit the dead incarnation's slots and refuse a fresh
# room's first, legitimate copy of a phrase the old room happened to leave at the cap (#734).
# The generation — store.room_generation, bumped on every (re)create, read by the write path
# from the SHARED sharded seq-state so every worker agrees on the incarnation without touching
# each other's rings — makes the recreated room a different key; the dead one is consulted by
# nothing and LRU-evicts under MAX_RING_ROOMS like any other idle room. The window needs no
# such thing: reaping requires IDLE_SECONDS (days) of quiet, so any window entry is already
# ancient and swept on first touch, and it can never be both a live rate and a reaped room.
DUPE_RING, DUPE_SHARE, MAX_RING_ROOMS = 64, 0.5, 512
_rings: OrderedDict[tuple[str, int], tuple[tuple[float, bytes], ...]] = OrderedDict()


def normalize_text(text: str) -> str:
    """The canonical form duplicate texts are keyed on: NFKC, invisibles to spaces,
    casefolded, whitespace collapsed.

    Not store.clean_text, on purpose, though the two share one invisible-category list.
    clean_text is a VALIDATOR: it raises on a text with nothing visible left, enforces
    the character cap, and preserves case and internal whitespace because storage must
    keep what the caller wrote. This is a KEY: it must never raise (a malformed text is
    append's to refuse, in append's words), and it must fold exactly the things a copy
    varies — case, spacing, Unicode compatibility forms. Storage keeps `A  b` and `a b`
    distinct; the duplicate ring cannot, or upper-casing one letter defeats it.

    The sweep rung is still here even though append sweeps too, because the unsigned
    lanes reach this BEFORE store.append runs clean_text — keying the unswept bytes
    there and the swept bytes on the signed lane would make one text two keys.

    Deliberately short of punctuation-stripping and of anything fuzzy — the measured
    flood is byte-identical after exactly this ladder, and every rung past it buys
    false positives on real messages without buying catches (measured: 0 additional
    duplicates caught by trailing-punctuation or digit masking). NFKC first, because
    compatibility forms must decompose before casefolding for the two to agree.
    """
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if unicodedata.category(c) in store.INVISIBLE_CATEGORIES else c for c in text
    )
    # A duplicate 422's ref token (app._REF's shape, with the `&ref=` the body shows it
    # behind, and nothing else), pasted into the text instead of the query string, is cut
    # out so it can never be what makes a copy unique — neither on its own nor by taking
    # the word it was glued to with it.
    return " ".join(re.sub(r"(?:&?ref=)?422-[\da-f]{1,8}-[\da-f]{4}", " ", text.casefold()).split())


def _dupe_key(room: str, text: str, min_length: int) -> tuple[str, bytes] | None:
    """The ring key for `text` in `room`, or None when the length floor exempts it.

    One function so reserving and releasing a copy can never disagree about what "the
    same text" is — a release that normalised differently would leak the reservation it
    was meant to hand back.
    """
    normalized = normalize_text(text)
    if len(normalized) < min_length:
        return None
    return (room, hashlib.blake2b(normalized.encode("utf-8"), digest_size=16).digest())


def dupe_refused(
    room: str,
    text: str,
    now: float,
    window: float,
    min_length: int = 16,
    max_copies: int = 5,
    cap: int = MAX_DUPE_KEYS,
    *,
    generation: int = 0,
) -> bool:
    """Whether this room should refuse `text` as the duplicate it now too obviously is,
    recording it as an accepted copy when it is not refused.

    Two rules, and either one refuses. The window: more than `max_copies` copies of one
    normalised text inside `window` seconds. The share cap: this copy would take more
    than DUPE_SHARE of the room's DUPE_RING most recent filterable messages, however long
    ago the last one landed — the window is a rate and a fixed-interval repeater simply
    posts below it, which is issue #697 and the block above it.

    Check-and-record in one call, on purpose: the write lanes run in a threadpool, so a
    check that returned and a record that ran later would let two concurrent copies of
    the Nth message both pass. Failing that way admits one extra copy — never a wrong
    refusal — which is the failure this whole module prefers.

    `window <= 0` is the off switch, short-circuited before the key is built, so a
    deployment that has opted out pays one comparison and no allocation — the opt-out
    buys back the pre-filter hot path exactly, which is the compatibility promise. It
    also takes no lock: an off filter cannot contend with anything.
    """
    key = _dupe_key(room, text, min_length) if window > 0 else None
    if key is None:
        return False  # off, or a short conversational repeat: legitimate by nature
    # The ring is keyed by the room's incarnation, not its bare name, so a reaped-and-
    # recreated room does not inherit the dead one's slots (#734, and the block above _rings).
    ring = (room, generation)
    with _dupes_lock:
        # A refused write is an access too. Refresh this ring's LRU slot on EVERY path
        # through the lock — share-cap refusal, window refusal, accepted write — so a room
        # under live duplicate attack is never mistaken for idle and evicted out from under
        # the very cap that is refusing its copies (Minh3132, #734). Move-only, guarded by
        # membership: the accepted write below stays the sole place a ring key is born, so
        # this records no slot and creates no empty ring. Mirrors _dupes, whose pop+reinsert
        # already refreshes it on the window's refusal path — _rings' two refusals both
        # return above that pop, so only the accepted write refreshed the ring before, which
        # let a room evicted while still under attack rebuild a fresh share allowance.
        if ring in _rings:
            _rings.move_to_end(ring)
        # The share cap first, and before anything is recorded: a refusal here may no more
        # extend a window than one below does, and the room's ring is the cheaper check.
        if sum(d == key[1] for _, d in _rings.get(ring, ())) + 1 > DUPE_SHARE * DUPE_RING:
            return True
        # Three touches of the pre-existing window logic below are not the share cap and
        # would not otherwise be in this change: `pop`-then-reinsert in place of `get` plus
        # a trailing `move_to_end`, the merged `seen is not None` branches, and the sweep's
        # `next(iter(...), None)`. They are here because core/limit.py sits ON its
        # sz-baseline.json cap and the ring had to be paid for in the same file — a real
        # constraint, not tidying. Each is behaviour-preserving with one exception, stated
        # so a reviewer does not have to find it: `pop`-then-reinsert refreshes the LRU
        # position on a REFUSAL, which the old `move_to_end` after the early return did
        # not. Harmless — a key being hammered is by definition the hot one, the cap still
        # holds the map, and no timestamp is added, so the window itself is untouched.
        live = tuple(t for t in _dupes.pop(key, ()) if now - t <= window)
        if len(live) >= max_copies:
            _dupes[key] = live[-max_copies:]  # prune, but never extend on a refusal
            return True
        _dupes[key] = (live + (now,))[-max_copies:]
        _rings[ring] = (_rings.pop(ring, ()) + ((now, key[1]),))[-DUPE_RING:]
        # Two bounds, because one is not enough under load: a per-call-capped sweep from
        # the oldest so a burst cannot turn one write into a pause, and the hard caps that
        # actually hold the memory whatever the sweep leaves behind. `any` rather than the
        # `max` an expiry check reaches for first: both are the negation of the original
        # `all(now - t > window ...)` on a live entry, but `max(())` RAISES on an empty
        # tuple where `all(())` and `any(())` do not. Nothing stores an empty tuple today
        # (max_copies is floored at 1 in config, and release drops the key instead), which
        # makes it exactly the kind of unreachable-until-it-is-not landmine to keep out of
        # a write path whose predecessor was total.
        for _ in range(8):
            oldest = next(iter(_dupes), None)
            if oldest is None or any(now - t <= window for t in _dupes[oldest]):
                break
            del _dupes[oldest]
        for mapping, bound in ((_dupes, cap), (_rings, MAX_RING_ROOMS)):
            while len(mapping) > bound:
                mapping.popitem(last=False)
    return False


def dupe_release(
    room: str, text: str, now: float, window: float, min_length: int = 16, *, generation: int = 0
) -> None:
    """Give back the copy `dupe_refused` recorded at `now`, because the write it was
    reserved for never landed.

    The reservation has to be taken before the append — that is what makes the check and
    the record one step — but the append refuses writes of its own: an invalid nick, a
    stale nonce, a text past the character cap, a full rooms directory. Without this,
    `max_copies` such failures would spend a room's whole window on a text nothing ever
    stored, and the next well-formed caller of that phrase would meet a 422 for copies
    that do not exist.

    ONE entry from each of the two structures the reservation touched, which is why they
    come back in one loop rather than two blocks: one timestamp from the window's tuple for
    this text, one (instant, digest) slot from the room's share ring. Both are matched on
    the reservation's OWN `now`, so a release only ever gives back the slot it added: a
    stale release cannot remove a later, successful copy of the same text, because that copy
    carries a different instant even though its digest is identical (the reviewer's race in
    #734). `list.remove` in both cases, removing exactly one match, so two reservations that
    collide on one time.monotonic() float cost one slot between them and the other survives.

    `generation` must be the one the reservation was keyed under, which the caller gets for
    free by reading it once and passing it to both calls — a release under a different
    incarnation would look in the wrong ring and hand nothing back (see _dupe_slot).

    Silent when either entry has already been swept or evicted — that is the same free
    window this would have opened, arrived at by another route. The ring slot matters as
    much as the timestamp because the share cap is cross-sender like everything else here:
    without it, failed writes of one phrase would leave a stranger's identical phrase
    refused against copies nothing ever stored. Re-inserted only when something is left,
    so neither map gains a key or an empty container from this path — dupe_refused stays
    the only place either one grows, and it is what holds them to their caps.
    """
    key = _dupe_key(room, text, min_length) if window > 0 else None
    if key is None:
        return
    with _dupes_lock:
        # Annotated `list[tuple]` because the two maps have different key and value types
        # and a checker reading the pair as a union rejects every line below it; the rule
        # being applied is identical for both, which is the whole reason they share a loop.
        both: list[tuple] = [(_dupes, key, now), (_rings, (room, generation), (now, key[1]))]
        for mapping, slot, entry in both:
            held = list(mapping.pop(slot, ()))
            if entry in held:
                held.remove(entry)
            if held:
                mapping[slot] = tuple(held)


def client_ip(request: Request, ip_header: str = "") -> str:
    """The socket peer, unless the operator has named a header to trust instead.

    No header is trusted by default. A forwarded-for header is a *claim by the client*; it
    becomes evidence only when the origin is unreachable except through the proxy that
    overwrites it. Trusting one unconditionally meant anyone who could reach the container
    directly got a fresh rate-limit identity per request for the cost of one header — the
    limiter, the write budget and the long-poll cap all key on this.

    X-Forwarded-For is never consulted implicitly, for the same reason plus one more:
    proxies *append* to it, so a client sending its own owns the first entry. An operator
    who really is behind such a proxy can still set CHAT_CLIENT_IP_HEADER=x-forwarded-for,
    but that is now a deliberate statement about their topology rather than a default.

    Shared by the rate limiter and the long-poll waiter cap: two per-IP bounds keyed on
    different notions of "IP" would each be bypassable by whichever header the other
    ignored.
    """
    if ip_header:
        forwarded = request.headers.get(ip_header, "").split(",")[0].strip()
        if forwarded:
            return forwarded
        return request.client.host if request.client else "?"
    # Not configured to read one. Note whether the request looks proxied anyway, so a
    # misconfiguration is visible in /stats instead of only in a support ticket.
    if any(h in request.headers for h in PROXY_IP_HEADERS):
        _proxy_evidence["proxied_requests"] += 1
    return request.client.host if request.client else "?"


def take(request, kind, per_min, burst=None, *, ip_header="", max_buckets=MAX_BUCKETS):
    """Token bucket per (client IP, kind). Returns (tokens left, seconds until the
    next one). Process-local: a real deployment puts the authoritative limit in the
    reverse proxy.

    `burst` is the bucket's capacity, and defaults to one minute's worth because that is
    what a per-minute budget means. A budget measured over a *day* needs the two apart:
    the capacity is the whole day's allowance and `per_min` is only the rate that hands it
    back. Folded together, a 20-rooms-per-day budget would be a bucket holding 0.0139
    tokens, which never reaches the 1.0 a grant costs — the limit would refuse everything.
    """
    ip = client_ip(request, ip_header)
    if len(_identities) < MAX_IDENTITIES:
        _identities.add(ip)
    now = time.monotonic()
    cap = float(per_min if burst is None else burst)
    tokens, last = _buckets.get((ip, kind), (cap, now))
    tokens = min(cap, tokens + (now - last) * per_min / 60.0)
    if tokens >= 1.0:  # granted: no wait, even when this was the last token
        tokens -= 1.0
        wait = 0.0
    else:
        wait = (1.0 - tokens) * 60.0 / per_min
    _buckets[(ip, kind)] = (tokens, now)
    _buckets.move_to_end((ip, kind))
    while len(_buckets) > max_buckets:
        _buckets.popitem(last=False)
    # Counted at the one point every rate-limited route already funnels through, so a new
    # route cannot forget to count itself. In-process, so these reset on restart — /stats
    # reports them next to `uptime_seconds`, which is what makes them readable.
    _requests[kind] = _requests.get(kind, 0) + 1
    _requests["rate_limited"] += bool(wait)
    config._dbg(1, "take", ip=ip, kind=kind, left=int(tokens), wait=round(wait, 3))
    return int(tokens), wait


def refund(request, kind, per_min, burst=None, *, ip_header="") -> None:
    """Hand one token back to the caller's bucket, capped at its burst.

    `last` is deliberately left alone: it is the refill clock, and moving it would either
    grant free time or discard earned time. Only the balance changes.
    """
    ip = client_ip(request, ip_header)
    cap = float(per_min if burst is None else burst)
    tokens, last = _buckets.get((ip, kind), (cap, time.monotonic()))
    _buckets[(ip, kind)] = (min(cap, tokens + 1.0), last)
    config._dbg(1, "refund", ip=ip, kind=kind)


# Set by the room-creation gate on the request it charged, read once by _settle_room_budget.
# On the scope rather than a module global because it is per-request state, and requests
# from one IP overlap: a module flag would be read by whichever request finished first.
CHARGED_CREATION = "_charged_room_creation"


def _settle_room_budget(request, record, rooms_per_day, *, ip_header="") -> None:
    """Refund the room-creation token if this request turned out not to create the room.

    The gate has to charge *before* the write — it exists to refuse a room before it comes
    into being — so when several callers send a first message to the same absent room at
    once, they all pass the existence check and all pay. Only one of them creates it; the
    rest append to a room that already exists by the time the store's create lock lets them
    through. That is not a rare shape either: agents converging on a shared rendezvous room
    is a documented pattern, and one swarm behind one NAT could spend a day's budget on a
    single room.

    `seq == 1` is the store's own answer to "did this call create the room": the record is
    the first line in the file. A room reaped and recreated starts at 1 again, which is
    correct — that really is a creation.
    """
    if request.scope.pop(CHARGED_CREATION, False) and record.get("seq") != 1:
        refund(request, "create", rooms_per_day / 1440.0, burst=rooms_per_day, ip_header=ip_header)


def refill_rate(per_min: int) -> str:
    """The refill, phrased so it stays meaningful at whatever limit is configured.

    `{per_min / 60:.1f} tokens/s` reads fine at the default 120/min and degrades to a flat
    "0.0 tokens/s" for anything under 30/min — a number an agent cannot pace against, on
    precisely the deployments that most need pacing. Under one per second the period is
    both accurate and the more useful form: "one every 30s" is a sleep, "0.03 tokens/s" is
    arithmetic the reader has to do first.
    """
    per_second = per_min / 60.0
    if per_second >= 1.0:
        return f"{per_second:.1f} tokens/s"
    return f"one token every {60.0 / per_min:.0f}s"


def limited(kind: str, per_min: int, retry_after: float, *, text, max_wait: float) -> Response:
    """429 an agent can act on. The retry delay is repeated in the *body* because
    most agent harnesses surface only the page text, never the headers.

    It also states the budget itself, which makes this response the primary way an agent
    learns the numbers: the manual deliberately does not name them (they are per
    deployment), so a caller that never reads /.well-known/agent.json still finds out what
    it is pacing against at the one moment the answer matters.
    """
    wait = max(1, round(retry_after))
    other = "write" if kind == "read" else "read"
    body = (
        f"429 rate limited: the {kind} budget for your IP ({per_min}/min) is spent.\n"
        f"retry after: {wait}s — the bucket refills continuously "
        f"({refill_rate(per_min)}), so waiting longer buys a bigger burst, up to "
        f"{per_min}.\n"
        f"still open: {other}s are a separate budget and are unaffected, and these paths "
        f"are never rate limited: {FREE_PATHS}.\n"
        f"cheaper pattern: poll /r/<room>?since=<last seq you saw> rather than refetching "
        f"the room, and prefer &wait={max_wait:g} to tight polling — one request per "
        f"{max_wait:g}s instead of twenty.\n"
        f"the enforced numbers are also published at /.well-known/agent.json under "
        f"limits.{kind}s_per_minute_per_ip."
    )
    r = text(body, 429)
    r.headers["Retry-After"] = str(wait)
    return r


def budget_note(kind: str, left: int, per_min: int) -> str:
    """Warn before the wall, not at it — and on a stride, so the warning stays shareable.

    The footer is one caller's pacing, so a reply carrying one is `no-store` and the CDN
    bypasses it (see the read paths in app.py). That is fine for a warning nobody sees
    twice, and it was not: a client polling at its ceiling sits permanently inside the
    last quarter of its budget, so *every* reply it got carried a footer and *none* of them
    could be cached. Measured on production, 47.7% of room reads were `bypass` at the edge
    against a 7.2% hit rate — the cache switched itself off for exactly the callers
    generating the most load.

    Lowering the threshold does not fix that: a client pinned at its ceiling is permanently
    inside whatever band is chosen. What fixes it is emitting on a *stride* of the remaining
    budget — roughly every `per_min // 24` requests, so about six warnings across the warning
    band and the rest of the replies shareable. At the production read budget of 600/min that
    is one footer every 25 requests; the other 24 can be served from the edge.

    The stride scales with the budget rather than being a constant, because a deployment with
    a small budget has no requests to spare: at anything under 24/min it is 1, which is the
    every-reply behaviour this replaces. A caller cannot step over a stride without landing
    on it, so the warning is never skipped, only thinned.

    `(left + 1) % stride` rather than `left % stride` so that the multiple is never *zero
    left*: a caller pinned at its ceiling is granted a token the instant one refills and
    would otherwise sit on the one value that always warns, which is the case this exists to
    remove. It lands on stride-1 instead — 24, 49, 74 … at 600/min.

    Reads only. A write reply is `no-store` whatever it carries — it mutates — so thinning
    its footer buys no cacheability and costs a writer its pacing. Sharing one helper made
    that easy to miss: at the production write budget of 300/min the stride is 12, which
    silently took write warnings from every in-band reply to 7.9% of them, and the test
    default of 30/min has a stride of 1 so nothing failed.
    """
    if left * 4 > per_min or (kind == "read" and (left + 1) % max(1, per_min // 24)):
        return ""
    return (
        f"\n# budget: {left} of {per_min} {kind}s left this minute "
        f"(refills {refill_rate(per_min)}; a 429 states the wait, and the full limits are "
        f"in /.well-known/agent.json)"
    )


# Long-poll bounds. `?wait=` holds a connection open, which is a cost model the
# request-counting rate limiter does not bound at all: 30 writes/min says nothing about
# how many sockets one caller may park. On a world-writable service that gap is the whole
# attack, so waiters are capped twice — per IP, and globally — and exceeding either
# degrades to an immediate empty reply rather than an error. A caller that cannot get a
# slot is exactly as well off as before long-polling existed.
MAX_WAITERS_TOTAL = config.MAX_WAITERS_TOTAL
MAX_WAITERS_PER_IP = config.MAX_WAITERS_PER_IP
_waiters_by_ip: dict[str, int] = {}
_waiters_total = 0


@contextmanager
def _waiter_slot(ip: str, max_total: int, max_per_ip: int):
    """Reserve one long-poll slot, or yield False when either cap is full.

    Plain integers, no lock: this is a single-threaded event loop, and every acquire and
    release happens without an await between the check and the mutation.
    """
    global _waiters_total
    if _waiters_total >= max_total or _waiters_by_ip.get(ip, 0) >= max_per_ip:
        yield False
        return
    _waiters_total += 1
    _waiters_by_ip[ip] = _waiters_by_ip.get(ip, 0) + 1
    try:
        yield True
    finally:
        _waiters_total -= 1
        left = _waiters_by_ip.get(ip, 1) - 1
        if left > 0:
            _waiters_by_ip[ip] = left
        else:
            _waiters_by_ip.pop(ip, None)  # never let the table grow per distinct IP


def waiter_note(ip: str, max_total: int, max_per_ip: int, wait: float) -> str:
    """Say that a long poll was refused a slot, so an instant empty reply is not misread.

    The degradation above stays: the caller gets the data it would have got anyway. What
    it could not get was the *reason* — an empty reply after a held ten seconds is the
    same bytes as one after no wait at all, so "sleep, then retry" and "poll straight
    back" look identical, and a caller with no other signal picks the second and spends
    its read budget at wire speed. Which cap was hit is named because the remedies differ:
    reduce your own concurrency, or wait for the instance to quieten.

    A sibling of `budget_note` on the same seam, but the fact is not `text/plain` only —
    `?format=json` carries the same verdict as the view's `wait_held`. `ip` is passed in
    because `client_ip` counts proxy evidence as a side effect.
    """
    mine = _waiters_by_ip.get(ip, 0)
    cause = (
        f"you hold all {max_per_ip} slots one caller may have"
        if mine >= max_per_ip
        else f"all {max_total} on this instance are busy"
    )
    return (
        f"\n# wait: not held — {cause}, so this reply is immediate rather than after "
        f"{wait:g}s. Sleep about that long before retrying; polling straight back re-reads "
        "the room for nothing and spends the budget you want for real reads."
    )
