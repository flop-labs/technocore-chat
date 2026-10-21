"""Map the `/stats` digest onto Prometheus metric families.

Scope is deliberately narrow: only what `store.service_stats` returns. `/stats` carries
five things this exporter does not publish — three from `service_stats` itself and two that
`app.py` adds to the view — and each omission is a decision rather than an oversight; see
OMITTED below.

Naming follows the Prometheus conventions: gauges carry no `_total`, counters do (the
client library appends it), and every byte figure is named `_bytes` because base units
are the convention rather than a preference.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from .fetch import DEFAULT_TIMEOUT, StatsUnavailableError, fetch_stats

log = logging.getLogger("technocore_exporter")

# The service's own counter names, mapped to (metric, help). `reaped_idle` and
# `reaped_stillborn` are folded into one family with a `reason` label, which is the only
# label in the whole exporter whose values come from the service rather than a constant —
# and it is bounded to exactly these two, because store.COUNTER_KEYS is a fixed tuple.
_REAP_REASONS = {"reaped_idle": "idle", "reaped_stillborn": "stillborn"}

_COUNTERS = {
    "messages": ("technocore_messages", "Messages appended to any room, lifetime."),
    "rooms_created": ("technocore_rooms_created", "Rooms created, lifetime."),
    "notes_written": ("technocore_notes_written", "Note writes, lifetime."),
    "topics_written": ("technocore_topics_written", "Room topic writes, lifetime."),
}

# What this exporter will not publish, and why. Kept as code-adjacent prose because the
# absences are the part a reviewer has to check.
#
# OMITTED: `requests`   — per *worker*, not per service. `_requests` is a plain module
#          dict, so under `--workers N` the digest reports roughly one worker's share
#          (src/app.py:1914 records it under-reporting by 3x once production moved to
#          `--workers 3`). Consecutive scrapes of a load-balanced URL can land on
#          different processes, so no arrangement of these samples is a monotonic
#          counter. Multiplying by `workers` is an estimate, not counter aggregation.
# OMITTED: `history`    — the stored samples. Prometheus stores the samples it scrapes;
#          re-exporting a ring as timestamp-labelled series would double-store it and
#          produce a second, disagreeing history. Only the newest sample's timestamp is
#          used, and only as a freshness gauge.
# OMITTED: `engagement` — pooled over the 50 most recently active rooms, so it is a
#          bounded-window sample rather than a service total. Publishing it beside real
#          totals invites an alert on a number that does not mean what its neighbours
#          mean.
#
# The last two are added to the view by `app.py`, not by `service_stats`, so they are
# outside this package's stated scope by construction — named anyway, because "three
# omissions" invites the reader to check and find five things in the digest:
#
# OMITTED: `capacity_limits` — request-shaping constants (message_chars, read_per_min and
#          friends), not occupancy. The two that actually bound the aggregates here are
#          already published from `service_stats`: `room_bytes_total` is the same constant
#          as `bytes.rooms_capacity` (technocore_room_bytes_capacity) and MAX_ROOMS
#          arrives as `rooms.capacity` (technocore_rooms_capacity).
# OMITTED: `client_identity` — `distinct_identities` counts a module-level dict, so it is
#          per *worker* for exactly the reason `requests` is, and `client_ip_header` is a
#          configuration string rather than a measurement.


def _rooms(rooms: dict) -> Iterable:
    """Room occupancy.

    The split between a label and separate metric names is the whole point of this
    function. `listed`/`unlisted` genuinely partition the room population, so they are
    label values on one metric and `sum by () (technocore_rooms_listing)` is correct.
    The class markers do not partition anything: `room_classes` composes by prefix, so
    `mb-p-x` is both a mailbox and unlisted (its docstring: `mb-p-x -> {mb, p}`), and a
    room can carry several markers at once. Summing those would double-count, so they
    are separate metric names — a shape in which nobody is tempted to add them up.
    """
    total = rooms.get("total", 0)
    yield GaugeMetricFamily(
        "technocore_rooms",
        "Rooms that exist, including unlisted ones. This is the figure the room cap bounds.",
        value=total,
    )
    yield GaugeMetricFamily(
        "technocore_rooms_capacity",
        "Maximum rooms this deployment will hold (store.MAX_ROOMS).",
        value=rooms.get("capacity", 0),
    )
    listing = GaugeMetricFamily(
        "technocore_rooms_listing",
        "Rooms by whether GET /rooms enumerates them. A true partition: these sum to technocore_rooms.",
        labels=["state"],
    )
    for state in ("listed", "unlisted"):
        listing.add_metric([state], rooms.get(state, 0))
    yield listing
    classes = GaugeMetricFamily(
        "technocore_rooms_class",
        "Rooms carrying each class marker. These OVERLAP and must never be summed: a name "
        "composes markers by prefix, so mb-p-x counts under mailbox and is also unlisted.",
        labels=["class"],
    )
    for name in ("mailbox", "ownable", "ephemeral"):
        classes.add_metric([name], rooms.get(name, 0))
    yield classes
    yield GaugeMetricFamily(
        "technocore_rooms_unclassified",
        "Rooms carrying no class marker at all. Not the complement of technocore_rooms_class, "
        "because those overlap.",
        value=rooms.get("open", 0),
    )


def _capacity(payload: dict) -> Iterable:
    """Byte and note gauges — the pressure an operator actually alerts on."""
    size = payload.get("bytes", {})
    notes = payload.get("notes", {})
    yield GaugeMetricFamily(
        "technocore_room_bytes",
        "Bytes held by room files.",
        value=size.get("rooms", 0),
    )
    yield GaugeMetricFamily(
        "technocore_room_bytes_capacity",
        "Byte budget rooms are held to (store.MAX_TOTAL_ROOM_BYTES). The enforced bound, "
        "not MAX_ROOMS * MAX_ROOM_BYTES.",
        value=size.get("rooms_capacity", 0),
    )
    yield GaugeMetricFamily(
        "technocore_note_bytes",
        "Bytes held by note files. Refreshed on create and otherwise at most one reap "
        "interval stale, so it is a gauge to watch rather than a bound a write is refused against.",
        value=size.get("notes", 0),
    )
    yield GaugeMetricFamily(
        "technocore_notes", "Notes stored across every namespace.", value=notes.get("total", 0)
    )
    yield GaugeMetricFamily(
        "technocore_notes_capacity",
        "Maximum notes across all namespaces (store.MAX_NOTES_TOTAL).",
        value=notes.get("capacity", 0),
    )
    yield GaugeMetricFamily(
        "technocore_notes_capacity_per_namespace",
        "Maximum notes in any one namespace (store.MAX_NOTES_PER_NS). A namespace can hit "
        "this while technocore_notes is far from its own cap.",
        value=notes.get("capacity_per_namespace", 0),
    )


def _counters(counters: dict) -> Iterable:
    """The six lifetime counters (store.COUNTER_KEYS).

    Best effort on both axes, and the HELP text says so because an operator who does not
    know it will read a flat line as an outage. They lag: message bumps are batched per
    worker and flushed on an interval or at shutdown, so a hard kill loses the tail. They
    can also reset: the file is rebuilt as zeros if it is lost, which Prometheus reads as
    a counter reset and handles, but which makes `increase()` over that window an
    undercount rather than a gap.
    """
    lag = " Best effort: batched per worker, so it lags, and resets to zero if the counter file is lost."
    for key, (name, help_text) in _COUNTERS.items():
        yield CounterMetricFamily(name, help_text + lag, value=counters.get(key, 0))
    reaped = CounterMetricFamily(
        "technocore_rooms_reaped",
        "Rooms removed by the reaper, by reason." + lag,
        labels=["reason"],
    )
    for key, reason in _REAP_REASONS.items():
        reaped.add_metric([reason], counters.get(key, 0))
    yield reaped


class TechnocoreCollector(Collector):
    """Scrapes /stats once per Prometheus scrape and maps it.

    The service caches that digest for CHAT_STATS_CACHE_SECONDS (60 by default) because
    building it is an O(cap) walk. Scraping faster than the cache therefore buys no
    freshness at all — it only spends requests — which is why the shipped scrape config
    sets 60s and why `technocore_stats_sample_age_seconds` is exported: it lets an
    operator see the staleness rather than assume it away.
    """

    def __init__(self, url: str, token: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._scrapes = {"success": 0, "error": 0}
        self._last_success = 0.0
        # `start_http_server` runs a ThreadingWSGIServer, so two overlapping scrapes call
        # collect() on this one instance concurrently.
        #
        # Two locks, because they are held for wildly different lengths of time. `_fetching`
        # spans a whole origin read and is acquired with a bound; `_state` guards the
        # counter bumps and is held for a few instructions. Folding them into one made the
        # failure path — which has to bump a counter precisely when it could not get the
        # fetch lock — unable to do so safely.
        #
        # `_fetching` serialises; it does not coalesce. The second scrape still makes its
        # own origin request once it holds the lock, so the guarantee is at most one request
        # in flight per process, not one request per pair of overlapping scrapes.
        # Deduplicating instead would mean serving one scrape a sample fetched for another,
        # which is a worse trade at a 60s scrape interval.
        #
        # What it must never do is push a scrape past the budget. Waiting here is time the
        # source timeout does not see, so an unbounded wait plus a full fetch took a second
        # scrape to ~2x the timeout — past Prometheus's scrape_timeout, which then stores
        # neither samples nor the failure telemetry the wait produced (reported by
        # @Minh3132 and @yukkie3276). `_gather` bounds the wait and the fetch against one
        # deadline instead, so a queued scrape reports failure inside the budget rather than
        # succeeding after nobody is listening.
        #
        # `_fetching` is released by the fetch worker, never by the scrape that started it —
        # see `_fetch_within`. That is what lets a scrape abandon a wedged fetch without
        # letting the next one open a second request to the same origin.
        self._fetching = threading.Lock()
        self._state = threading.Lock()

    def collect(self) -> Iterable:
        """Yield the page. Never raises — see _gather."""
        yield from self._gather()

    def _gather(self) -> list:
        """Build every family for one scrape, or the failure telemetry alone.

        A list rather than a generator, and this is the point rather than a style choice.
        `collect()` being a generator meant the try/except below only ever guarded the
        fetch: the mapping ran after the block, on the consumer's iteration, so any error
        in it escaped and the client library answered /metrics with a 500. Building the
        families inside the guard is what makes "no failure escapes" true of the mapping
        as well as the transport.

        One deadline covers everything after `started`: the wait for the fetch lock, and
        then the fetch itself in every phase of it — resolution, connect, headers and body
        — because `_fetch_within` stops waiting at the deadline rather than trusting the
        transport to. That is what makes the ceiling in server.py an actual bound: whatever
        else happens, this returns within the configured source timeout, so the failure it
        reports arrives while Prometheus is still listening for it.
        """
        started = time.monotonic()
        deadline = started + self._timeout
        if not self._fetching.acquire(timeout=self._timeout):
            # Queued behind a scrape that used the whole budget. Reported as a failure
            # rather than waited out: past here there is no time left to read the origin
            # in, and an answer after the scrape timeout is worth less than a fast 0.
            return self._failed(started, "timed out waiting for the in-flight scrape")
        try:
            payload = self._fetch_within(deadline)
            families = [
                *_rooms(payload.get("rooms", {})),
                *_capacity(payload),
                *_counters(payload.get("counters", {})),
                *self._sample_age(payload),
            ]
        except StatsUnavailableError as exc:
            # No re-raise and no detail in a metric: a failed scrape is reported as
            # success=0 and an error count, and the reason goes to the log. Encoding
            # it as a label value would let the origin's failure mode drive the labels.
            return self._failed(started, str(exc))
        except Exception:
            # Deliberately broad, and the narrow handlers in fetch.py are still the
            # real answer. This is the structural one: a scrape that receives a 500
            # learns nothing, not even that the exporter is alive. Three transport
            # families have now escaped a narrow handler here (a bare socket timeout,
            # http.client.HTTPException, and ConnectionResetError mid-body), so the
            # assumption that any such list is complete has been wrong three times.
            log.exception("unexpected error reading %s", self._url)
            return self._failed(started, None)
        with self._state:
            self._scrapes["success"] += 1
            self._last_success = time.time()
        return families + list(self._self_metrics(time.monotonic() - started, ok=True))

    def _fetch_within(self, deadline: float) -> dict:
        """The digest, or refuse at the deadline — whatever the transport is doing.

        `fetch_stats` bounds itself, and for the phase it can see it does so correctly. It
        cannot see the others. `urlopen(timeout=)` is a *per socket operation* timeout, and
        three phases run before the body read that `_read_within` guards:

          * name resolution. `socket.create_connection` calls `getaddrinfo()` before it has
            a socket for the timeout to apply to, so a wedged resolver blocks the whole
            call — 20.0s of a 3s budget, measured at /metrics (reported by @Minh3132);
          * connect. That same function then tries *every* address the name resolved to,
            giving each the full timeout, so a hostname with four blackholed A records cost
            12.0s of a 3s budget, four times the budget for four records; and
          * the status line and response headers, read by `http.client` before `open()`
            returns. An origin trickling one header byte per 0.5s never trips a 3s socket
            timeout: 22.6s, still waiting when the origin stopped. This one needs no
            hostname at all — it lands on the shipped `127.0.0.1` default.

        Enumerating those three and bounding each is the move this file has already been
        wrong about three times over (see the broad handler in `_gather` and the trailing
        `OSError` in fetch.py). So the bound here is structural: the fetch runs on a worker
        and the scrape stops waiting at the deadline, whatever the worker is blocked on.

        The lock is the reason this is safe rather than a thread leak. `_fetching` is
        already held when we get here, and the *worker* releases it — so an abandoned fetch
        keeps it, the next scrape's bounded `acquire` fails fast and reports
        `scrape_success 0` inside its own budget, and no second request is ever opened to an
        origin that has not answered the first. One wedged origin therefore costs one live
        thread at a time, not one per scrape.
        """
        # Clamped rather than checked for exhaustion: a scrape that wins the lock with
        # nothing left gets a zero timeout, which is a non-blocking socket, which fails
        # immediately and is published as scrape_success 0 inside the budget — the same
        # answer an explicit branch here would produce, without a branch that only the
        # clock can reach and no test can honestly cover.
        remaining = max(0.0, deadline - time.monotonic())
        box: dict = {}
        done = threading.Event()
        abandoned = threading.Event()

        def run() -> None:
            try:
                box["payload"] = fetch_stats(self._url, self._token, remaining)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the scrape's thread
                # Re-raised below rather than handled, so `_gather`'s existing handlers see
                # exactly what they saw when the fetch ran inline.
                box["error"] = exc
            finally:
                # Before `done`, so a scrape that wakes on it finds the lock already free.
                self._fetching.release()
                done.set()
                if abandoned.is_set():
                    # The scrape that started this has already answered, so nothing above
                    # will ever look at `box`. Without this the origin's real reason is
                    # lost: the operator gets "exceeded the scrape budget", which says the
                    # answer was late but not that the resolver was down. Running the fetch
                    # inline used to put that reason in the log, and it should still.
                    #
                    # Always an error, never a discarded digest. `fetch_stats` is given
                    # this same deadline, so a worker that outran the scrape has outrun its
                    # own budget too and `_read_within` refuses before it returns a body.
                    # The default below is defensive, not a case that happens.
                    log.warning(
                        "scrape of %s finished after the budget: %s",
                        self._url,
                        box.get("error", "answer discarded"),
                    )

        worker = threading.Thread(target=run, name="technocore-exporter-fetch", daemon=True)
        try:
            worker.start()
        except BaseException:
            # Nothing will release `_fetching` if the worker never ran, and a collector
            # that can no longer take its own lock answers every later scrape with the
            # queued-behind failure forever.
            self._fetching.release()
            raise
        if not done.wait(max(0.0, deadline - time.monotonic())):
            # Set before raising, so the worker knows nobody is left to report what it
            # finds. A worker that finishes inside this window sees it unset and stays
            # quiet, which is right: the scrape below is about to report for it.
            abandoned.set()
            raise StatsUnavailableError("exceeded the scrape budget before the origin answered")
        if "error" in box:
            raise box["error"]
        return box["payload"]

    def _failed(self, started: float, reason: str | None) -> list:
        """Failure telemetry alone, and the count that goes with it.

        `started` is taken before the fetch lock, so the duration published here includes
        any time spent queued. That is deliberate: the queueing is what pushed a scrape
        past the budget, and a duration measured from the moment the lock was won reported
        9.01s for a scrape that took 17.82s — the one metric that would have shown the
        problem was the one metric blind to it.
        """
        if reason:
            log.warning("scrape of %s failed: %s", self._url, reason)
        with self._state:
            self._scrapes["error"] += 1
        return list(self._self_metrics(time.monotonic() - started, ok=False))

    def _sample_age(self, payload: dict) -> Iterable:
        """Age of the newest stored sample.

        This is the only thing taken from `history`, and it is not the age of the figures
        above: those come from a cache at most CHAT_STATS_CACHE_SECONDS old, while the
        stored samples are written at most every SNAPSHOT_EVERY (300s) and kept for
        SNAPSHOT_KEEP_SECONDS (30h). It is named for the sample rather than for the scrape
        because of that gap.

        Do not alert on it. Sampling is driven by writes, not by a timer, so a service
        nobody is writing to has an unboundedly old newest sample while being perfectly
        healthy — measured at 300.6s on an idle probe instance moments after the last
        write. `technocore_exporter_scrape_success` is the availability signal; this is
        context for reading the history, and a floor under how stale a `history`-derived
        number can be.
        """
        history = payload.get("history")
        if not isinstance(history, list) or not history:
            return
        newest = history[-1]
        stamp = newest.get("t") if isinstance(newest, dict) else None
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return
        yield GaugeMetricFamily(
            "technocore_stats_sample_age_seconds",
            "Age of the newest stored aggregate sample. Samples are written by writes, at "
            "most one per 300s — so this grows without bound on an idle service and is NOT "
            "an availability signal. Not the age of the gauges above, which come from a "
            "separate 60s cache.",
            value=max(0.0, time.time() - float(stamp)),
        )

    def _self_metrics(self, duration: float, ok: bool) -> Iterable:
        """Whether the exporter itself is working — the first thing to alert on.

        Without these, a broken token is indistinguishable from a service with no rooms:
        both render as an absence of samples, and `absent()` alerts are the ones people
        forget to write.

        The counters are snapshotted under `_state` rather than read field by field while
        yielding: this runs outside the fetch lock now, so another scrape can bump them
        between two of the families below and publish a page that disagrees with itself.
        """
        with self._state:
            last_success = self._last_success
            counts = dict(self._scrapes)
        yield GaugeMetricFamily(
            "technocore_exporter_scrape_success",
            "1 if the most recent /stats read succeeded, 0 otherwise.",
            value=1 if ok else 0,
        )
        yield GaugeMetricFamily(
            "technocore_exporter_scrape_duration_seconds",
            "Wall time of the most recent /stats read, including any time queued behind "
            "another scrape. Bounded by TECHNOCORE_STATS_TIMEOUT.",
            value=duration,
        )
        yield GaugeMetricFamily(
            "technocore_exporter_last_success_timestamp_seconds",
            "Unix time of the last successful /stats read; 0 if there has not been one.",
            value=last_success,
        )
        scrapes = CounterMetricFamily(
            "technocore_exporter_scrapes",
            "/stats reads attempted by this exporter process, by outcome.",
            labels=["outcome"],
        )
        for outcome in ("success", "error"):
            scrapes.add_metric([outcome], counts[outcome])
        yield scrapes
