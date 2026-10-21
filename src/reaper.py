"""The worker lifecycle: start the reap loop on the way up, flush batched counters on the
way out.

Outside the core on purpose. `_reap` and `_bump` are store primitives and stay there; what
lives here is only *when* they run, which is a property of how the service is deployed —
a uvicorn worker with a lifespan — rather than of the store. This module schedules store
calls and decides nothing a store caller could observe differently.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.concurrency import run_in_threadpool

import config
import store


@asynccontextmanager
async def lifespan(_app):
    """Start this worker's reaper thread on the way up; stop it and flush this worker's
    batched counter deltas on the way out.

    The reaper is `_reap_forever` on a daemon thread (#896). The pass used to run
    inline in whichever write crossed REAP_EVERY first, which made that one request wait for
    a walk of the whole store. Daemon, because a pass can take minutes and must not hold up a
    graceful shutdown: one cut short is a crashed worker's pass, which the next one repairs.

    `store._bump` lets a plain message ride in memory until something structural, the
    message bound or a snapshot flushes it (#588). Nothing else flushes a worker that is
    still under the bound when it is told to stop, so without this an ordinary rolling
    deploy — SIGTERM, which uvicorn turns into a graceful shutdown — would drop what each
    worker was holding, not just a worker killed hard. That hard-kill window stays: no
    shutdown hook runs for SIGKILL, and the counters are best effort by contract.
    """
    stop = threading.Event()
    threading.Thread(target=_reap_forever, args=(config.ROOT, stop), daemon=True).start()
    yield
    stop.set()
    await run_in_threadpool(store._bump, config.ROOT)


def _reap_forever(root: Path, stop: threading.Event) -> None:
    """Call `_reap` every REAP_EVERY until `stop` is set — the pass's only caller outside
    tests, on the daemon thread `lifespan` starts in each worker (#896).

    It used to run inline, before the write, in `_write_record` and `note_set`, so whichever
    ordinary write crossed the interval first carried the whole walk: minutes on the live
    store, past the edge's 100 s origin timeout, holding a request thread and contending with
    request writes for `.counters.lock` (#588) the whole time. The marker's throttle and lock
    are unchanged, and every worker runs this loop, so how many passes land in one interval
    is the throttle's business, not this loop's (#948).

    It waits before its first pass: a worker that has just started has no reason to walk the
    store ahead of the throttle, and a test that starts the app gets no pass racing it. A
    pass that raises is reported and the loop carries on — a dead reaper thread would stop
    retirement for the life of the worker, where an inline failure only ever cost one write.
    """
    while not stop.wait(store.REAP_EVERY):
        try:
            store._reap(root)
        except Exception as error:  # noqa: BLE001 - the next interval retries; dying would not
            config._dbg(0, "reap_failed", error=repr(error))  # level 0: always printed
