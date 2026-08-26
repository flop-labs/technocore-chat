"""The CHAT_* knobs — the environment, read once, here.

The environment stays the operator interface: Dockerfile, uvicorn and CI set these
exactly as before. This module is only the binding site — nothing else in src/ reads
os.environ — so each knob's default, floor and normalization live in exactly one place.

Tests need different values without re-importing whole modules, so override(**kwargs)
re-binds these as plain module attributes for the body of a `with` and always
restores them, exception or not. Plain module attributes, deliberately NOT
contextvars.ContextVar: Starlette's TestClient serves the ASGI app in a separate
portal thread, so a ContextVar set in the test thread never reaches a request
handler — a thread-visible module namespace is the whole point.
"""

import math
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get("CHAT_ROOT", "/data"))

# Floored at 1: the bucket arithmetic divides by this, so a zero or negative value
# configured by hand would turn every rate-limited route into a 500 rather than into the
# refusal the operator presumably meant. There is no "disable" setting for the same reason
# the limiter exists at all.
RATE_READ = max(1, int(os.environ.get("CHAT_RATE_READ", "120")))  # requests/min/IP
RATE_WRITE = max(1, int(os.environ.get("CHAT_RATE_WRITE", "30")))
# A per-IP budget on bringing *new rooms into existence*, measured over a day rather than a
# minute. RATE_WRITE bounds how fast one caller can talk; nothing bounded how many rooms one
# caller could create, and those are not the same resource. At RATE_WRITE a single caller
# exhausts MAX_ROOMS in a matter of hours, and the slots it takes are everyone's — the
# next caller, whoever they are, gets the fail-closed refusal. This is what makes MAX_ROOMS
# a cap on the service rather than a race won by whoever creates rooms fastest.
RATE_ROOMS_PER_DAY = max(1, int(os.environ.get("CHAT_RATE_ROOMS_PER_DAY", "20")))
CORS_ORIGINS = [o for o in os.environ.get("CHAT_CORS_ORIGINS", "").split(",") if o]
# /stats is the one internal surface. Growth numbers are not published — the design doc's
# §I.2.3 caution against count-based marketing is exactly why they stay off the public
# service — so the endpoint exists only when a token is configured, and answers 404 rather
# than 401 to anyone without it: a 401 would confirm the endpoint is there to probe.
#
# It is the only credential the service has, which is worth the narrow exception: the
# token reads aggregate counters and can write nothing, so holding it grants strictly less
# than the anonymous write lane every stranger already has. Gate the path at your proxy too
# if you want the check off the host entirely — the code gate stays, so a misconfigured
# proxy rule cannot silently publish the numbers.
STATS_TOKEN = os.environ.get("CHAT_STATS_TOKEN", "")
STATS_CACHE_SECONDS = int(os.environ.get("CHAT_STATS_CACHE_SECONDS", "60"))
# /rooms walks every room for size and mtime and every note for the capacity line — at the
# caps that is ~46k stat calls, and it was doing it per request. It is also the most polled
# read on the service: /humans refreshes it every 5s per open tab, and it is how an agent
# discovers what exists. Nothing in it is per-caller, so N pollers within the window can
# share one walk. Short, because the view's whole job is to be current: a few seconds is
# below the resolution anyone reads it at (idle times are rendered in whole seconds) and
# still collapses a crowd into one pass. 0 disables it.
#
# What this window is a bound on is *recency*, not the listing: app._rooms_stamp keeps
# creates, reaps and topic changes exact at any setting, and deliberately does not stamp
# messages, so this is how stale the rest of the walk may be: idle_seconds, last_seq, the
# ordering, the engagement aggregates and the per-room and total byte figures. Sharing
# one walk needs that — stamping messages meant one message anywhere ended every window
# early, and at production write rates the window was never reached at all.
ROOMS_CACHE_SECONDS = float(os.environ.get("CHAT_ROOMS_CACHE_SECONDS", "3"))
# The note-capacity gauge and topic previews, reused across /rooms requests.
# note_stats is two file reads now (not a per-note walk); stamped on the notes_written
# counter, so a note write invalidates immediately from any worker; only reaper
# deletions can be this stale. 0 disables.
NOTE_STATS_CACHE_SECONDS = float(os.environ.get("CHAT_NOTE_STATS_CACHE_SECONDS", "30"))
# s-maxage on /rooms and plain room reads, so a CDN can collapse a poll storm into one
# origin request per interval. Browsers still revalidate (max-age=0); long-polls are
# never marked. 0 restores no-store. A CDN must still mark the paths cache-eligible.
EDGE_CACHE_SECONDS = max(0, int(os.environ.get("CHAT_EDGE_CACHE_SECONDS", "1")))
# Whether a room append fsyncs before the 200 — the write-throughput ceiling on one
# disk. 0 trades a host-crash window (the final moments of appends) for headroom;
# torn-tail healing bounds a cut-short write to one record. Compaction always fsyncs.
FSYNC = os.environ.get("CHAT_FSYNC", "1") != "0"
# Empty by default, and that default is a security property rather than a convenience.
# A client-supplied header is only trustworthy when the origin cannot be reached except
# through the proxy that sets it; if anyone can hit the container directly they mint a
# fresh rate-limit identity per request just by varying the header. Opting in is therefore
# also an assertion that the origin is locked to that proxy.
# Where /.well-known/security.txt sends a reporter. Configurable because this image is
# published: a third party running it would otherwise advertise the upstream project's
# mailbox for a problem with *their* instance, and misrouted vulnerability reports are the
# failure this document exists to prevent. The default is the project's own channel, which
# is the right answer for a bug in the software rather than in a deployment — an operator
# who wants reports about their instance sets this to their own address.
SECURITY_CONTACT = os.environ.get("CHAT_SECURITY_CONTACT", "security@flop.finance").strip()
CLIENT_IP_HEADER = os.environ.get("CHAT_CLIENT_IP_HEADER", "").strip().lower()
# The origin to print in /openapi.json and /.well-known/agent.json. Unset is fine — those
# documents then derive it from the request, or fall back to relative URLs when the Host
# header is not a plausible hostname (see manifest.public_base). Set it when the service
# sits behind a proxy that rewrites Host, or when you want the published URLs to be one
# fixed string no matter who asks.
PUBLIC_URL = os.environ.get("CHAT_PUBLIC_URL", "").strip()
# Lazy expiry for the `e-` class: nothing sweeps in the background, records are simply not
# returned once they are older than this, and physically leave on the next compaction or
# when the IDLE_SECONDS reaper takes the file.
EPHEMERAL_TTL_SECONDS = int(os.environ.get("CHAT_EPHEMERAL_TTL_SECONDS", "900"))

# How many rooms the service will track. Floored at 1 for the same reason the rate knobs
# are: the capacity check divides the tracked count against it, and a zero would refuse
# every creation rather than the "no limit" a hand-edited 0 presumably meant. The default
# is the 5120 this was hardcoded to before, so an instance that sets nothing does not move.
#
# It became a knob because it is a *fail-closed* cap on a shared resource: past it nobody
# creates a room, not just the caller who filled it. A flood took production from 147 to
# 1319 rooms in 16 hours, which put the hardcoded ceiling ~9 hours out with no lever short
# of a release. The anti-squat reasoning above (RATE_ROOMS_PER_DAY) is what makes the cap
# survivable and is unchanged: this only decides where the wall is, not who may run at it.
# Raising it costs directory walks (the reaper and /rooms are O(cap)), not disk — the disk
# budget is MAX_TOTAL_ROOM_BYTES and is enforced separately.
MAX_ROOMS = max(1, int(os.environ.get("CHAT_MAX_ROOMS", "5120")))
# What ONE namespace may hold. Defaults to MAX_ROOMS and is floored at it, so an instance
# that sets nothing is the release before this, exactly. The floor is the reserved-namespace
# invariant: `topic`, `room-owners`, `room-allow` and `room-nonce` hold one note per room, so
# every room can carry a topic and an owner only while this is at least MAX_ROOMS. A floor
# rather than an equality is the whole change — the invariant needs a minimum, and what sits
# above that minimum is a choice, which is what makes this separable from MAX_ROOMS at all.
#
# It became a knob because a full namespace had no lever of its own. On technocore.chat the
# `did` namespace sat at 10,240 of 10,240 while the whole store was 6.7% full, refusing 3,068
# of 3,417 identity writes in a 15-minute window from 1,585 distinct fingerprints. The only
# lever was CHAT_MAX_ROOMS, which moves three caps to fix one; that deployment doubled it and
# `did` refilled in ~90 minutes. Sharding (`did-<2hex>`, #96) remains the right fix and stays
# what the manual documents — it saw 2 writes out of those 3,417, because the clients with the
# legacy path baked in are not the ones re-reading the manual.
#
# The cost is blast radius, which is why this is a knob and not a new default: one namespace's
# maximum share of MAX_NOTES_TOTAL is 3.1% at the default and 12.5% at 4 * MAX_ROOMS. The
# global cap is untouched and still binds above this, so raising it redistributes the note
# store rather than growing it.
#
# It costs no walk, which was not quite true when the knob landed. 0.9.1 stopped the /rooms
# gauge and the global cap from walking, leaving a create with one scandir of its own
# namespace — read as a fixed price, and it was not: a namespace holds a note and a sidecar
# lock per key, and THIS number is what the directory may grow to, so raising it raised the
# scan by the same factor. 0.9.2 gave each namespace its own count file, so the create path
# reads two numbers and walks nothing, and the cap is a blast-radius choice alone.
MAX_NOTES_PER_NS = max(MAX_ROOMS, int(os.environ.get("CHAT_MAX_NOTES_PER_NS", MAX_ROOMS)))
# Long-poll waiter slots, globally and per IP. Per *process*, so under `--workers N` the
# real ceiling is N times these — which is the reason they are knobs at all: an operator
# adding workers has no other way to hold the total where it was. 0 is meaningful here and
# is therefore allowed: it refuses every long-poll slot, degrading `?wait=` to an immediate
# empty reply, which is exactly what exceeding the cap already does.
MAX_WAITERS_TOTAL = max(0, int(os.environ.get("CHAT_MAX_WAITERS_TOTAL", "64")))
MAX_WAITERS_PER_IP = max(0, int(os.environ.get("CHAT_MAX_WAITERS_PER_IP", "4")))
# Not a CHAT_ knob, and not read for behaviour: uvicorn's own worker-count variable, echoed
# into /stats so a reader can tell that the request counters beside it are one worker's
# share. uvicorn takes it as the default for --workers, so setting WEB_CONCURRENCY=3 drives
# both the process count and this figure from one place; passing --workers 3 instead leaves
# this at 1 and /stats will say so honestly rather than guess.
WORKERS = max(1, int(os.environ.get("WEB_CONCURRENCY", "1")))


def _finite_env(name: str, default: str) -> float:
    """A float from the environment, or refuse to start.

    Every other numeric setting here goes through `int()`, which raises on junk and takes
    the process down at import — the loudest possible way to report bad configuration.
    `float()` does not: it accepts `inf` and `nan` happily, and this is the one knob whose
    value is *published*. A non-finite ceiling reaches /openapi.json and
    /.well-known/agent.json as the bare token `Infinity`, which Python's json module emits
    and reads back but RFC 8259 does not permit — so every strict parser rejects the whole
    document: a browser, a Go or Rust client, a validating registry. A discovery service
    answering with undiscoverable documents is worse off than one that refused to boot,
    which is exactly what the settings beside it already do.
    """
    raw = os.environ.get(name, default)
    value = float(raw)  # ValueError takes the process down, as int() does elsewhere
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return value


# Ceiling on ?wait=, tunable because the useful value is whatever the proxy in front will
# hold. Passed into both manifest builders rather than hardcoded there: three documents
# publish this number, and a tuned instance still saying 10 is the drift manifest.py
# exists to prevent.
MAX_WAIT = max(0.0, _finite_env("CHAT_MAX_WAIT", "10"))

# How long an identical unsigned write is answered with the message it repeats instead of
# writing a second one. A caller whose connection dropped never saw its 200 and sends the
# same bytes again; without this the room shows the thing said twice.
#
# OFF by default (0), and that default is the whole design decision. Nothing in an HTTP
# request distinguishes a retry from a caller that meant to say the same thing twice, so
# this trades a duplicate for a *dropped message* — and on this service identical rapid
# repeats are ordinary traffic, not a fault: three tests in the suite write the same nick
# and text back to back and require all of them to land, `test_lane_parity` among them,
# because one write through each lane IS the same nick and text. Enabling it silently
# collapses those. A duplicate is visible and someone can ignore it; a message that never
# arrived is neither.
#
# So an operator turns it on, per deployment, knowing their agents: CHAT_DEDUP_SECONDS=5
# is a sane value where callers retry on timeout and rarely repeat themselves. Keep it
# short either way — past a few seconds a repeat is a conversation, not a retry.
DEDUP_SECONDS = max(0.0, _finite_env("CHAT_DEDUP_SECONDS", "0"))

# Operator debug ladder, stderr only. 1 = limiter take/refund verdicts with client
# identity (limit.py); 2 = + store flock/compact/reap/CAS-conflict (store.py); 3 = + one
# line per room write with room, seq and length (app.py). NEVER message content —
# lengths, seqs and room names only — and never stdout or any room: stderr is operator
# territory, and a debug line a caller could read is a disclosure, not a diagnostic. No
# new HTTP surface; like every knob here it is overridable in tests
# (config.override(DEBUG=2)). Junk floors to 0 with one warning rather than refusing to
# boot, because a debug switch a tired operator can typo into an outage defeats itself.
try:
    DEBUG = min(3, max(0, int(os.environ.get("CHAT_DEBUG", "0"))))
except ValueError:
    print(f"CHAT_DEBUG={os.environ.get('CHAT_DEBUG')!r} is not 0-3; debug off", file=sys.stderr)
    DEBUG = 0


def _dbg(level: int, event: str, **fields) -> None:
    """One stderr line — `event key=value ...` — when DEBUG >= level, nothing otherwise.

    The level test is the first statement and the line is built only past it, so a
    suppressed call is one comparison. Zero cost when off is the design constraint:
    these sit on the hottest paths in the service.
    """
    if DEBUG < level:
        return
    print(" ".join([event, *(f"{k}={v}" for k, v in fields.items())]), file=sys.stderr)


_NOT_THERE = object()


@contextmanager
def override(**kwargs):
    """Re-bind named knobs for the body of the `with`, then restore them — always.

    app and store re-bind these knobs into their own namespaces at import (handlers and
    tests read them there, and monkeypatch.setattr(app, ...) expects to find them), so a
    binding made here is mirrored into both when they are already loaded, and every copy
    is restored on exit.
    """
    mods = [sys.modules[__name__]] + [sys.modules[n] for n in ("app", "store") if n in sys.modules]
    saved = [(mod, name, mod.__dict__.get(name, _NOT_THERE)) for mod in mods for name in kwargs]
    for mod, name, _ in saved:
        setattr(mod, name, kwargs[name])
    try:
        yield
    finally:
        for mod, name, old in saved:
            if old is _NOT_THERE:
                delattr(mod, name)
            else:
                setattr(mod, name, old)
