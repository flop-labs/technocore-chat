"""What the exporter publishes, and — more of the file than usual — what it does not.

The omissions are the reviewable part of this package: an exporter that ships a misleading
metric is worse than one that ships nothing, because an alert gets written against it.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families
from technocore_exporter.collector import TechnocoreCollector
from technocore_exporter.fetch import StatsUnavailableError


class _Collector(TechnocoreCollector):
    """The collector with the network replaced by a value, or an exception."""

    def __init__(self, payload=None, error=None):
        super().__init__("http://origin.invalid/stats", "token")
        self._payload = payload
        self._error = error

    def _read(self):
        if self._error is not None:
            raise self._error
        return self._payload


@pytest.fixture
def render(stats, monkeypatch):
    """Render the exposition page for a given digest, or for a failed read.

    The module-level `fetch_stats` is what gets replaced, not a method on the collector, so
    every test drives `collect()` by the same path production does.
    """

    def _render(payload=None, error=None):
        collector = _Collector(payload if error is None else None, error)
        monkeypatch.setattr(
            "technocore_exporter.collector.fetch_stats",
            lambda *a, **k: collector._read(),
        )
        registry = CollectorRegistry()
        registry.register(collector)
        return generate_latest(registry).decode()

    return lambda payload=stats, error=None: _render(payload, error)


def _families(text: str) -> dict:
    return {f.name: f for f in text_string_to_metric_families(text)}


def _sample(text: str, name: str, **labels) -> float:
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    raise AssertionError(f"no sample {name}{labels or ''} in output")


def test_the_output_is_valid_prometheus_exposition(render):
    """A parser round-trip, not a string compare: this is the check promtool also makes."""
    text = render()
    families = _families(text)
    assert families, "nothing parsed"
    for family in families.values():
        assert family.documentation, f"{family.name} has no HELP"
        assert family.type in {"gauge", "counter", "unknown"}


def test_the_listing_labels_partition_the_room_total(render, stats):
    """listed + unlisted == total, on real numbers. Summing this label is correct."""
    text = render()
    listed = _sample(text, "technocore_rooms_listing", state="listed")
    unlisted = _sample(text, "technocore_rooms_listing", state="unlisted")
    total = _sample(text, "technocore_rooms")
    assert listed == stats["rooms"]["listed"] == 4
    assert unlisted == stats["rooms"]["unlisted"] == 2
    assert listed + unlisted == total == 6


def test_the_class_counts_do_not_partition_and_say_so(render, stats):
    """The trap this exporter exists to not fall into.

    The fixture holds a room that is both a mailbox and unlisted, and one that is unlisted
    with no other marker — so the class counts fall short of the total, and a dashboard
    that summed them would under-report occupancy while looking arithmetically tidy.
    """
    text = render()
    classes = sum(
        _sample(text, "technocore_rooms_class", **{"class": name})
        for name in ("mailbox", "ownable", "ephemeral")
    )
    unclassified = _sample(text, "technocore_rooms_unclassified")
    total = _sample(text, "technocore_rooms")
    assert classes + unclassified == 5
    assert total == 6
    assert classes + unclassified < total, "fixture no longer exercises the overlap"
    help_text = _families(text)["technocore_rooms_class"].documentation
    assert "OVERLAP" in help_text and "never be summed" in help_text


def test_every_label_value_is_from_a_fixed_set(render):
    """No room name, namespace, DID, IP or URL may ever become a label value.

    Asserted as an allowlist rather than a denylist: a denylist passes for whatever nobody
    thought of, and the whole point is that this label set is closed.
    """
    allowed = {
        "state": {"listed", "unlisted"},
        "class": {"mailbox", "ownable", "ephemeral"},
        "reason": {"idle", "stillborn"},
        "outcome": {"success", "error"},
    }
    for family in text_string_to_metric_families(render()):
        for s in family.samples:
            for key, value in s.labels.items():
                assert key in allowed, f"unexpected label {key!r} on {s.name}"
                assert value in allowed[key], f"unexpected value {value!r} for {key}"


def test_per_worker_request_counters_are_not_exported(render, stats):
    """`requests` is per worker; consecutive scrapes of a load-balanced URL can land on
    different processes, so no arrangement of those samples is a monotonic counter."""
    assert "requests" in stats, "fixture should still carry the field being refused"
    text = render()
    for forbidden in ("technocore_requests", "technocore_read", "technocore_rate_limited"):
        assert forbidden not in text
    assert "uptime_seconds" not in text
    assert "per_worker" not in text


def test_history_is_not_republished_as_series(render, stats):
    """Only the newest sample's timestamp is used, and only as freshness."""
    assert stats["history"], "fixture should carry at least one stored sample"
    text = render()
    assert "technocore_history" not in text
    assert _sample(text, "technocore_stats_sample_age_seconds") >= 0


def test_engagement_is_not_exported(render, stats):
    """A bounded-window sample, not a service total — it must not sit beside real ones."""
    assert "engagement" in stats
    text = render()
    for forbidden in ("engagement", "zero_response", "nick_diversity", "windowed_messages"):
        assert forbidden not in text


def test_counters_are_counters_and_gauges_are_gauges(render):
    """`_total` on counters and not on gauges is the convention promtool enforces."""
    families = _families(render())
    assert families["technocore_messages"].type == "counter"
    assert families["technocore_rooms"].type == "gauge"
    text = render()
    assert "technocore_messages_total" in text
    assert "technocore_rooms_total" not in text


def test_the_reap_reason_label_carries_both_values(render, stats):
    text = render()
    assert _sample(text, "technocore_rooms_reaped_total", reason="idle") == 0
    assert _sample(text, "technocore_rooms_reaped_total", reason="stillborn") == 0
    assert stats["counters"]["reaped_idle"] == 0


def test_the_reap_reasons_are_not_transposed(render, stats):
    """The fixture has both reap counters at 0, so no other test can tell them apart.

    With `reaped_idle == reaped_stillborn == 0`, swapping the two values in `_REAP_REASONS`
    passes every other assertion in this file — including the one above, which checks both
    samples are 0. The mapping direction is pinned here against distinct numbers instead.
    """
    counters = {**stats["counters"], "reaped_idle": 7, "reaped_stillborn": 3}
    text = render(payload={**stats, "counters": counters})
    assert _sample(text, "technocore_rooms_reaped_total", reason="idle") == 7
    assert _sample(text, "technocore_rooms_reaped_total", reason="stillborn") == 3


def test_the_real_values_are_carried_through(render, stats):
    """The mapping itself, against the captured digest."""
    text = render()
    assert _sample(text, "technocore_messages_total") == stats["counters"]["messages"] == 9
    assert _sample(text, "technocore_rooms_created_total") == 5
    assert _sample(text, "technocore_notes_written_total") == 5
    assert _sample(text, "technocore_topics_written_total") == 3
    assert _sample(text, "technocore_room_bytes") == stats["bytes"]["rooms"] == 1200
    assert _sample(text, "technocore_note_bytes") == 63
    assert _sample(text, "technocore_notes") == 4
    assert _sample(text, "technocore_notes_capacity") == 163840
    assert _sample(text, "technocore_notes_capacity_per_namespace") == 5120
    assert _sample(text, "technocore_rooms_capacity") == 5120
    assert _sample(text, "technocore_room_bytes_capacity") == 5368709120


def test_counter_help_states_the_lag_and_reset_behaviour(render):
    """An operator who does not know these are batched reads a flat line as an outage."""
    doc = _families(render())["technocore_messages"].documentation
    assert "Best effort" in doc and "lags" in doc and "resets to zero" in doc


def test_a_failed_scrape_reports_zero_and_still_serves_self_metrics(render):
    """The failure mode that must not look like an empty service.

    Without these, a wrong token and a service with no rooms are the same absence of
    samples — and `absent()` alerts are the ones people forget to write.
    """
    text = render(error=StatsUnavailableError("status 404 (wrong or unset CHAT_STATS_TOKEN?)"))
    assert _sample(text, "technocore_exporter_scrape_success") == 0
    assert _sample(text, "technocore_exporter_scrapes_total", outcome="error") == 1
    assert "technocore_rooms " not in text, "no stale storage gauges on a failed scrape"


def test_the_failure_reason_never_reaches_a_metric(render):
    """The origin's failure text must not drive the label set."""
    text = render(error=StatsUnavailableError("unreachable: [Errno -2] Name or service not known"))
    assert "Errno" not in text and "not known" not in text


def test_an_unexpected_error_still_publishes_a_failed_scrape(render):
    """`collect()` must never propagate, whatever the cause.

    An exception escaping here makes the client library answer /metrics with a 500, so the
    scrape learns nothing — not even that the exporter is alive. Two transport families
    have already escaped a narrow handler in fetch.py (a bare socket timeout, and
    http.client.HTTPException), so the broad catch is the structural answer rather than a
    third guess at a complete list.
    """
    text = render(error=RuntimeError("something nobody predicted"))
    assert _sample(text, "technocore_exporter_scrape_success") == 0
    assert _sample(text, "technocore_exporter_scrapes_total", outcome="error") == 1
    assert "technocore_rooms " not in text
    assert "nobody predicted" not in text, "the cause goes to the log, never to a metric"


def test_a_bool_timestamp_is_not_read_as_a_number(render, stats):
    """`True` is an int in Python; a sample age of 'now minus True' would be nonsense."""
    payload = {**stats, "history": [{"t": True}]}
    assert "technocore_stats_sample_age_seconds" not in render(payload=payload)


# ------------------------------------------------------- found in self-review, not by CI


def test_a_mapping_error_cannot_escape_collect(render, stats, monkeypatch):
    """The guard has to cover the mapping, not only the fetch.

    `collect()` was a generator whose try/except wrapped `fetch_stats` alone, so the
    mapping ran later, on the consumer's iteration, and any error in it escaped — leaving
    /metrics answering 500, which is exactly what the broad catch exists to prevent. The
    families are built inside the guard now. Reproduced before the fix by making one
    mapping function raise.
    """
    import technocore_exporter.collector as module

    monkeypatch.setattr(module, "_capacity", lambda p: (_ for _ in ()).throw(KeyError("bug")))
    text = render()
    assert _sample(text, "technocore_exporter_scrape_success") == 0
    assert "technocore_rooms " not in text, "a partial page is worse than a failed one"
    assert "bug" not in text, "the cause goes to the log, never to a metric"


def test_overlapping_scrapes_are_serialised(stats, monkeypatch):
    """`start_http_server` is threaded, so collect() runs concurrently on one instance.

    Asserted as non-overlap rather than as a final count: `self._scrapes[...] += 1` is an
    unguarded read-modify-write without the lock — the same shape as the `_buckets` race in
    core's limiter — but a count assertion can pass by luck, since losing a bump needs the
    threads to interleave on exactly that bytecode. Depth is deterministic: if any two
    bodies ever overlap, the maximum observed depth is 2 and the test fails every time.

    Serialising is not deduplicating, and the last assertion below is what says so: eight
    threads produce eight successful reads, one after another. The lock bounds how many
    origin requests are in flight at once, not how many are made.
    """
    import technocore_exporter.collector as module

    depth = 0
    peak = 0
    seen = threading.Lock()

    def tracked_fetch(*a, **k):
        nonlocal depth, peak
        with seen:
            depth += 1
            peak = max(peak, depth)
        time.sleep(0.01)
        with seen:
            depth -= 1
        return stats

    monkeypatch.setattr(module, "fetch_stats", tracked_fetch)
    collector = TechnocoreCollector("http://origin.invalid/stats", "token")
    threads = [threading.Thread(target=lambda: list(collector.collect())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a scrape deadlocked"
    assert peak == 1, f"{peak} scrapes overlapped; the collector lock is not holding"
    assert collector._scrapes["success"] == 8


def test_a_digest_with_no_stored_samples_publishes_no_sample_age(render, stats):
    """A service that has never written a snapshot, which is every service on day one.

    `app.py` builds the view as `{**service_stats, "history": store.snapshots(root)}` and
    `snapshots()` returns [] until the first one is written, so this is the ordinary
    fresh-deployment shape rather than a malformed digest. `history` is deliberately not in
    REQUIRED — its absence is a fact about the service, not a broken read — so the scrape
    must succeed and simply omit the age.

    It was the one branch in the package no test drove; the storage gauges beside it are
    what make the omission safe to leave silent.
    """
    text = render(payload={**stats, "history": []})
    assert "technocore_stats_sample_age_seconds" not in text
    assert _sample(text, "technocore_exporter_scrape_success") == 1
    assert _sample(text, "technocore_rooms") == stats["rooms"]["total"]


# --------------------------------------------------- the scrape budget, under contention


def _hanging_origin():
    """An origin that accepts and then says nothing at all. Returns its port.

    A real socket, not a patched `fetch_stats`: the property under test is how long the
    collector takes to answer, and a stub that sleeps proves only that a sleep sleeps.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    held = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:  # pragma: no cover - the test finished first
                return
            held.append(conn)  # kept open and unanswered

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1]


def _race(collector, count=2):
    """Drive `count` overlapping scrapes, returning (elapsed, families) for each in order."""
    results = {}

    def scrape(name, delay):
        time.sleep(delay)
        started = time.monotonic()
        families = list(collector.collect())
        results[name] = (time.monotonic() - started, families)

    threads = [threading.Thread(target=scrape, args=(i, i * 0.05)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return [results[i] for i in range(count)]


def _value(families, name, *labels):
    """A sample by its exposed name — `technocore_exporter_scrapes_total`, not the family
    name `technocore_exporter_scrapes` the client library derives it from."""
    for family in families:
        for sample in family.samples:
            if sample.name == name and tuple(sample.labels.values()) == labels:
                return sample.value
    raise AssertionError(f"no sample {name}{labels or ''}")


def test_a_queued_scrape_still_answers_inside_the_budget():
    """Reported by @Minh3132 and @yukkie3276, on the same head, half an hour apart.

    `_gather` serialises scrapes, and the wait was time the source timeout never saw: a
    second scrape waited a full timeout for the lock and then spent another one on its own
    fetch. Measured at 17.82s for a 9s timeout — past the shipped `scrape_timeout: 15s`, so
    Prometheus abandoned it and stored neither samples nor the `scrape_success 0` the wait
    had just produced. One deadline now covers the wait and the fetch together.
    """
    collector = TechnocoreCollector(
        f"http://127.0.0.1:{_hanging_origin()}/stats", "token", timeout=1.0
    )
    for elapsed, families in _race(collector):
        assert _value(families, "technocore_exporter_scrape_success") == 0
        assert elapsed < 1.5, f"a 1.0s budget took {elapsed:.2f}s"


def test_the_published_duration_is_the_time_the_scrape_actually_took():
    """The metric that would have shown the queueing was the metric blind to it.

    `started` was taken after the fetch lock was won, so the queued scrape published
    `scrape_duration_seconds 9.01` for a scrape that took 17.82s to answer. An operator
    watching the duration could not see the wait that was busting their scrape timeout.
    Taking `started` before the acquire is what makes this number the answer time.
    """
    collector = TechnocoreCollector(
        f"http://127.0.0.1:{_hanging_origin()}/stats", "token", timeout=1.0
    )
    for elapsed, families in _race(collector):
        published = _value(families, "technocore_exporter_scrape_duration_seconds")
        assert abs(published - elapsed) < 0.25, (
            f"published {published:.2f}s for a scrape that took {elapsed:.2f}s"
        )


def test_a_scrape_that_never_wins_the_lock_still_reports_inside_the_budget():
    """The far end of the queue: a scrape that cannot get the lock at all.

    Driven by holding `_fetching` outright rather than by racing two slow origins, because
    the property is "the wait is bounded" and a race can only ever demonstrate the waits it
    happened to produce. Without the bound this call blocks for as long as the holder does
    — indefinitely — and Prometheus abandons it long before it answers.
    """
    collector = TechnocoreCollector("http://origin.invalid/stats", "token", timeout=0.5)
    collector._fetching.acquire()
    try:
        started = time.monotonic()
        families = list(collector.collect())
        elapsed = time.monotonic() - started
    finally:
        collector._fetching.release()
    assert _value(families, "technocore_exporter_scrape_success") == 0
    assert _value(families, "technocore_exporter_scrapes_total", "error") == 1
    assert elapsed < 1.0, f"a 0.5s budget took {elapsed:.2f}s waiting for the lock"


def _trickling_header_origin(seconds=4.0, every=0.5):
    """An origin that sends a status line and then header bytes, slowly. Returns its port.

    The half of the trickle the body-read fix does not reach: `http.client` reads the status
    line and headers inside `urlopen`, under the per-socket-operation timeout, so a byte
    every 0.5s under a 1.0s budget never trips anything. It stops after `seconds` rather
    than running forever, for the reason the body-trickle test gives: without the fix this
    has to fail rather than wedge CI.

    `every` must stay comfortably under the caller's budget — that is the whole mechanism.
    A gap wider than the budget is trapped by the per-socket timeout like any other slow
    read, and demonstrates nothing.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    connections = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:  # pragma: no cover - the test finished first
                return
            connections.append(conn)
            threading.Thread(target=trickle, args=(conn,), daemon=True).start()

    def trickle(conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            # A header line that never reaches the blank line ending the headers.
            pad = b"X-Padding: " + b"a" * 4096 + b"\r\n"
            started = time.monotonic()
            i = 0
            while time.monotonic() - started < seconds:
                conn.sendall(pad[i % len(pad) : i % len(pad) + 1])
                i += 1
                time.sleep(every)
        except OSError:  # pragma: no cover - the scrape gave up first, which is the point
            pass

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1], connections


def test_a_trickling_header_cannot_outrun_the_budget():
    """The body deadline guards the body. Headers are read before it ever runs.

    `_read_within` bounds `response.read1`, but `urlopen` has already returned by then —
    `http.client` consumed the status line and every header under the per-socket timeout,
    which one byte per 0.5s never trips. Measured at 22.6s of a 3s budget at `/metrics`,
    and it was still going when the origin gave up rather than the exporter.

    This is the one of the three that needs no hostname: it lands on the shipped
    `127.0.0.1` default, because it is about what the origin sends rather than how it is
    addressed.
    """
    port, _ = _trickling_header_origin()
    collector = TechnocoreCollector(f"http://127.0.0.1:{port}/stats", "token", timeout=1.0)
    started = time.monotonic()
    families = list(collector.collect())
    elapsed = time.monotonic() - started
    assert _value(families, "technocore_exporter_scrape_success") == 0
    assert elapsed < 1.5, f"a 1.0s budget took {elapsed:.2f}s reading headers"


def test_a_wedged_resolver_cannot_outrun_the_budget(monkeypatch):
    """Reported by @Minh3132 on `0deacdf`.

    `socket.create_connection` calls `getaddrinfo()` before it has a socket for
    `urlopen(timeout=)` to apply to, so name resolution is outside every bound this package
    had. Measured at 20.0s of a 3s budget at `/metrics`, with no sample stored by
    Prometheus at all: past `scrape_timeout: 15s`, the failure telemetry arrives after
    nobody is listening for it.

    The resolver is stubbed rather than pointed at a real wedged one because a test cannot
    depend on the host's DNS being broken in a particular way; what it blocks in is real
    time, on the thread CPython would really block.
    """
    real = socket.getaddrinfo

    def wedged(host, *args, **kwargs):
        if host == "origin.invalid":
            time.sleep(4.0)
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return real(host, *args, **kwargs)  # pragma: no cover - only the stub host is used

    monkeypatch.setattr(socket, "getaddrinfo", wedged)
    collector = TechnocoreCollector("http://origin.invalid:8080/stats", "token", timeout=1.0)
    started = time.monotonic()
    families = list(collector.collect())
    elapsed = time.monotonic() - started
    assert _value(families, "technocore_exporter_scrape_success") == 0
    assert elapsed < 1.5, f"a 1.0s budget took {elapsed:.2f}s resolving"


def test_every_address_of_a_name_cannot_each_spend_the_whole_budget(monkeypatch):
    """`create_connection` tries every address a name resolved to, with the full timeout each.

    So the budget is spent once per address record, not once per scrape: four blackholed A
    records cost 12.0s of a 3s budget, each connect logged with the whole 3.0s rather than
    a share. Two records is enough to break the shipped arithmetic, provided both blackhole
    rather than refuse — a refusal returns at once, a dropping firewall does not. At the
    accepted ceiling of just under 10s, two such addresses reach ~20s against
    `scrape_timeout: 15s`.

    Connect is stubbed because no address is portably guaranteed to blackhole rather than
    refuse in CI. What is *not* stubbed is the part being pinned: the addresses come out of
    the real `create_connection` loop, and the timeout recorded on each socket is the one it
    really set. The assertion on `attempts` is that fact; the assertion on elapsed is that
    the collector no longer pays for it.
    """
    addresses = [f"198.51.100.{n}" for n in (1, 2, 3, 4)]
    attempts = []

    def resolves_to_all(host, port, *args, **kwargs):
        assert host == "origin.invalid"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

    def blackhole(self, address):
        attempts.append((address[0], self.gettimeout()))
        time.sleep(self.gettimeout())
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "getaddrinfo", resolves_to_all)
    monkeypatch.setattr(socket.socket, "connect", blackhole)
    collector = TechnocoreCollector("http://origin.invalid:8080/stats", "token", timeout=0.5)
    started = time.monotonic()
    families = list(collector.collect())
    elapsed = time.monotonic() - started
    assert _value(families, "technocore_exporter_scrape_success") == 0
    assert elapsed < 1.0, f"a 0.5s budget took {elapsed:.2f}s connecting"
    # Wait for the abandoned worker to walk the rest of the addresses, polling the fact
    # rather than sleeping a guessed duration: a fixed sleep that expires early lets
    # monkeypatch restore the real `connect`, and the worker then makes a genuine outbound
    # attempt to a TEST-NET address from CI.
    until = time.monotonic() + 10.0
    while len(attempts) < len(addresses) and time.monotonic() < until:
        time.sleep(0.02)
    # What CPython actually did with the budget: every address, each with the whole of what
    # was left of it rather than a share — which is what makes four addresses cost four.
    assert [ip for ip, _ in attempts] == addresses
    assert min(timeout for _, timeout in attempts) > 0.45, (
        f"expected the whole budget on each address, got {attempts}"
    )


def test_an_abandoned_fetch_keeps_the_lock_so_no_second_request_is_opened():
    """The thread bound, stated as a property rather than left to the reader.

    `_fetching` is released by the worker, not by the scrape that started it. A scrape that
    abandons a wedged fetch therefore leaves the lock held, and every later scrape fails on
    the bounded acquire instead of opening a second request to an origin that has not
    answered the first. Without that, a wedged origin would collect one live thread and one
    in-flight request per scrape, for as long as it stayed wedged.

    This guards the fix's own hazard rather than a pre-existing bug, so it is written
    against a blocker the worker cannot escape on its own: the trickling headers, which are
    outside every timeout `fetch_stats` can set. An origin that merely hangs is no good
    here — the worker's socket timeout fires at about the same instant the scrape abandons
    it, and the lock is then released a few microseconds either side of the assertion.
    """
    port, connections = _trickling_header_origin(seconds=3.0, every=0.1)
    collector = TechnocoreCollector(f"http://127.0.0.1:{port}/stats", "token", timeout=0.3)
    for _ in range(5):
        families = list(collector.collect())
        assert _value(families, "technocore_exporter_scrape_success") == 0
    # Counted at the origin rather than as live threads. A thread count is a count over the
    # whole process, and the other tests in this file deliberately leave wedged fetches
    # behind them — so it measures the neighbours, not this collector. Connections accepted
    # by *this* origin are the invariant in the docstring, stated directly.
    assert len(connections) == 1, f"five scrapes opened {len(connections)} requests"
    assert collector._fetching.locked(), "the abandoned fetch released the lock"


def test_a_fetch_thread_that_cannot_start_does_not_strand_the_lock(monkeypatch):
    """The one path where the worker cannot be the one to release `_fetching`.

    Handing the release to the worker is what bounds a wedged origin to a single in-flight
    request. It also means that if the thread never starts — `RuntimeError: can't start new
    thread`, which is what thread exhaustion looks like — nobody releases, and a collector
    that cannot take its own lock answers *every* later scrape with the queued-behind
    failure, permanently, from one transient failure to spawn.
    """

    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    collector = TechnocoreCollector("http://origin.invalid/stats", "token", timeout=1.0)
    families = list(collector.collect())
    assert _value(families, "technocore_exporter_scrape_success") == 0
    assert not collector._fetching.locked(), "a failed spawn stranded the fetch lock"


def test_an_abandoned_fetch_still_reports_what_the_origin_finally_did(caplog, monkeypatch):
    """Running the fetch on a worker takes the transport's real reason out of the log.

    The scrape answers at the deadline with "exceeded the scrape budget", which says the
    answer was late but not *why* — and the worker that eventually learns the reason has no
    one left to tell. Inline, `_failed` put `unreachable: [Errno -3] Temporary failure in
    name resolution` in front of the operator. That is a diagnostic this change would
    otherwise have removed, so the worker logs it once it knows.
    """
    real = socket.getaddrinfo

    def wedged(host, *args, **kwargs):
        if host == "origin.invalid":
            time.sleep(1.0)
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        return real(host, *args, **kwargs)  # pragma: no cover - only the stub host is used

    monkeypatch.setattr(socket, "getaddrinfo", wedged)
    collector = TechnocoreCollector("http://origin.invalid:8080/stats", "token", timeout=0.2)
    with caplog.at_level("WARNING", logger="technocore_exporter"):
        families = list(collector.collect())
        assert _value(families, "technocore_exporter_scrape_success") == 0
        # The scrape is already back; wait for the worker to reach its own conclusion.
        # Waited out inside the patch rather than after it, so the worker never sees the
        # real resolver restored underneath it.
        until = time.monotonic() + 10.0
        while collector._fetching.locked() and time.monotonic() < until:
            time.sleep(0.02)
    messages = [record.getMessage() for record in caplog.records]
    assert any("after the budget" in m and "name resolution" in m for m in messages), messages


def test_an_origin_that_answers_late_is_still_an_error_not_a_discarded_digest(caplog, monkeypatch):
    """An abandoned worker cannot come back with a usable digest, and that is worth pinning.

    `fetch_stats` is handed the same deadline the scrape waits on, so a worker that outran
    the scrape has outrun its own budget too: `_read_within` refuses before the body is
    returned. That is why the abandoned-worker log has one arm rather than two, and it is
    the kind of claim that rots silently — a later change giving the fetch its own longer
    budget would make a late success reachable, and this test is what would notice.

    The origin here is entirely healthy. Only the resolution is slow, and it is slow in the
    one phase `fetch_stats` cannot bound for itself, so the fetch really does go on to
    connect and read a valid digest before the deadline check refuses it.
    """
    body = json.dumps({"rooms": {}, "bytes": {}, "notes": {}, "counters": {}}).encode()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def serve():
        try:
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
            conn.sendall(body)
            conn.close()
        except OSError:  # pragma: no cover - the test finished first
            pass

    threading.Thread(target=serve, daemon=True).start()
    port = listener.getsockname()[1]
    real = socket.getaddrinfo

    def slow_then_fine(host, requested_port, *args, **kwargs):
        if host == "origin.invalid":
            time.sleep(0.8)
            return real("127.0.0.1", port, *args, **kwargs)
        return real(host, requested_port, *args, **kwargs)  # pragma: no cover - stub only

    monkeypatch.setattr(socket, "getaddrinfo", slow_then_fine)
    collector = TechnocoreCollector("http://origin.invalid:8080/stats", "token", timeout=0.2)
    with caplog.at_level("WARNING", logger="technocore_exporter"):
        families = list(collector.collect())
        assert _value(families, "technocore_exporter_scrape_success") == 0
        until = time.monotonic() + 10.0
        while collector._fetching.locked() and time.monotonic() < until:
            time.sleep(0.02)
    assert not collector._fetching.locked(), "the worker never finished"
    late = [
        record.getMessage()
        for record in caplog.records
        if "origin.invalid" in record.getMessage() and "after the budget" in record.getMessage()
    ]
    assert late, [record.getMessage() for record in caplog.records]
    assert "exceeded the scrape budget while reading" in late[0], late
    assert "answer discarded" not in late[0], late
