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
ROOMS_CACHE_SECONDS = float(os.environ.get("CHAT_ROOMS_CACHE_SECONDS", "3"))
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
