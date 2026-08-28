"""Run: uv run --group dev python -m pytest tests

The caps that became `CHAT_*` knobs in 0.9.0, checked the way test_docs.py checks
`CHAT_MAX_WAIT`: by booting a fresh interpreter through the real import chain rather than
by reading `config` in this process. Two things have to hold and only the second is
obvious — the environment moves the number, *and* an unset environment leaves it exactly
where it was. The second is what makes the change safe to deploy: every instance that sets
nothing must behave identically to the release before it.

Reading `config.MAX_ROOMS` here would prove neither. The value that matters is the one
`store` and `app` ended up bound to, and those re-bind at import — a knob that config
parses but store never picks up is the failure this guards.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[2] / "src")

# One boot, every binding site: config parses, store and limit re-bind, app aliases from
# limit. Anything that reads its own copy shows up here as a disagreement.
PROBE = (
    f"import sys; sys.path.insert(0, {SRC!r}); "
    "import json, app, config, limit, store; "
    "print(json.dumps({"
    "'config.MAX_ROOMS': config.MAX_ROOMS, 'store.MAX_ROOMS': store.MAX_ROOMS, "
    "'config.MAX_NOTES_PER_NS': config.MAX_NOTES_PER_NS, "
    "'store.MAX_NOTES_PER_NS': store.MAX_NOTES_PER_NS, "
    "'store.MAX_NOTES_TOTAL': store.MAX_NOTES_TOTAL, "
    "'config.MAX_WAITERS_TOTAL': config.MAX_WAITERS_TOTAL, "
    "'limit.MAX_WAITERS_TOTAL': limit.MAX_WAITERS_TOTAL, "
    "'app.MAX_WAITERS_TOTAL': app.MAX_WAITERS_TOTAL, "
    "'config.MAX_WAITERS_PER_IP': config.MAX_WAITERS_PER_IP, "
    "'limit.MAX_WAITERS_PER_IP': limit.MAX_WAITERS_PER_IP, "
    "'app.MAX_WAITERS_PER_IP': app.MAX_WAITERS_PER_IP, "
    "'config.WAIT_POLL': config.WAIT_POLL, 'app.WAIT_POLL': app.WAIT_POLL, "
    "'config.DUPE_FILTER_SECONDS': config.DUPE_FILTER_SECONDS, "
    "'app.DUPE_FILTER_SECONDS': app.DUPE_FILTER_SECONDS, "
    "'config.DUPE_MIN_LENGTH': config.DUPE_MIN_LENGTH, "
    "'app.DUPE_MIN_LENGTH': app.DUPE_MIN_LENGTH, "
    "'config.DUPE_MAX_COPIES': config.DUPE_MAX_COPIES, "
    "'app.DUPE_MAX_COPIES': app.DUPE_MAX_COPIES, "
    "'config.WORKERS': config.WORKERS}))"
)


def boot(**env: str) -> dict:
    """Import the real chain in a fresh interpreter with `env` applied, or fail loudly."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    clean.pop("WEB_CONCURRENCY", None)
    run = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env={**clean, **env}
    )
    assert run.returncode == 0, f"boot failed: {run.stderr}"
    return json.loads(run.stdout)


def test_the_new_knobs_default_to_the_values_they_replaced() -> None:
    """An instance that sets nothing does not move. Each of these was hardcoded before the
    release that made it a knob, and every deployment already running is that instance."""
    values = boot()
    assert values["config.MAX_ROOMS"] == 5120
    assert values["config.MAX_NOTES_PER_NS"] == 5120  # = MAX_ROOMS, the number it replaced
    assert values["config.MAX_WAITERS_TOTAL"] == 64
    assert values["config.MAX_WAITERS_PER_IP"] == 4
    assert values["config.WORKERS"] == 1  # no WEB_CONCURRENCY set


def test_the_environment_moves_them_at_every_binding_site() -> None:
    """…and reaches the modules that actually enforce them. store gates room creation on
    its own `MAX_ROOMS`, and app passes its own `MAX_WAITERS_*` into `limit._waiter_slot`,
    so a knob that stopped at `config` would parse cleanly and change nothing."""
    values = boot(
        CHAT_MAX_ROOMS="99",
        CHAT_MAX_NOTES_PER_NS="400",
        CHAT_MAX_WAITERS_TOTAL="7",
        CHAT_MAX_WAITERS_PER_IP="2",
        WEB_CONCURRENCY="3",
    )
    assert values["config.MAX_ROOMS"] == values["store.MAX_ROOMS"] == 99
    # Its own knob now, so the two move independently — and the per-namespace cap reaches
    # `store`, which is the module `_check_note_capacity` enforces it from.
    assert values["config.MAX_NOTES_PER_NS"] == values["store.MAX_NOTES_PER_NS"] == 400
    for module in ("config", "limit", "app"):
        assert values[f"{module}.MAX_WAITERS_TOTAL"] == 7
        assert values[f"{module}.MAX_WAITERS_PER_IP"] == 2
    assert values["config.WORKERS"] == 3


def test_the_floors_hold() -> None:
    """MAX_ROOMS floors at 1 like the rate knobs: the capacity check compares against it,
    and a hand-edited 0 would refuse every creation rather than lift the limit. The waiter
    caps floor at 0 instead, and 0 is a real setting — it refuses every long-poll slot,
    which is what exceeding the cap already does, so `?wait=` degrades to an immediate
    empty reply rather than erroring."""
    assert boot(CHAT_MAX_ROOMS="0")["config.MAX_ROOMS"] == 1
    assert boot(CHAT_MAX_ROOMS="-5")["config.MAX_ROOMS"] == 1
    floored = boot(CHAT_MAX_WAITERS_TOTAL="-1", CHAT_MAX_WAITERS_PER_IP="-1")
    assert floored["config.MAX_WAITERS_TOTAL"] == 0
    assert floored["config.MAX_WAITERS_PER_IP"] == 0
    # MAX_NOTES_PER_NS floors at MAX_ROOMS rather than at a literal, and that floor is an
    # invariant and not a typo guard: the four reserved namespaces (topic, room-owners,
    # room-allow, room-nonce) hold one note per room each, so anything under MAX_ROOMS means
    # some room cannot carry a topic or an owner. A value below it clamps up, silently and on
    # purpose — the alternative is refusing to boot over a setting whose intent was "smaller".
    assert boot(CHAT_MAX_NOTES_PER_NS="1")["config.MAX_NOTES_PER_NS"] == 5120
    assert boot(CHAT_MAX_NOTES_PER_NS="-5")["config.MAX_NOTES_PER_NS"] == 5120
    clamped = boot(CHAT_MAX_ROOMS="99", CHAT_MAX_NOTES_PER_NS="10")
    assert clamped["config.MAX_NOTES_PER_NS"] == 99  # the floor follows MAX_ROOMS, not 5120


def test_the_per_namespace_note_cap_moves_without_dragging_the_others() -> None:
    """The whole point of the knob: today the only lever on a full namespace is
    CHAT_MAX_ROOMS, which moves three caps to fix one — the room count, the global note cap
    derived from it, and this. Raised on its own it must widen ONE namespace's share and
    leave the store's total where it was, so the global cap still binds above it."""
    base, widened = boot(), boot(CHAT_MAX_NOTES_PER_NS="20480")
    assert widened["config.MAX_NOTES_PER_NS"] == 20480 == 4 * base["config.MAX_NOTES_PER_NS"]
    assert widened["config.MAX_ROOMS"] == base["config.MAX_ROOMS"]
    assert widened["store.MAX_NOTES_TOTAL"] == base["store.MAX_NOTES_TOTAL"]
    # Still the outer bound: one namespace may now take 12.5% of the store instead of 3.1%,
    # which is the cost the knob buys, but it cannot take more than the store holds.
    assert widened["store.MAX_NOTES_TOTAL"] > widened["config.MAX_NOTES_PER_NS"]


def test_junk_in_the_new_knob_refuses_to_boot() -> None:
    """`int()` again: a per-namespace cap that silently fell back to its default would leave
    an operator raising it during an incident believing they had."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    run = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env={**clean, "CHAT_MAX_NOTES_PER_NS": "plenty"},
    )
    assert run.returncode != 0, "app booted with a non-numeric CHAT_MAX_NOTES_PER_NS"
    assert "ValueError" in run.stderr


def test_junk_refuses_to_boot() -> None:
    """`int()` raises at import, which takes the process down — the loudest way to report
    bad configuration, and what every other numeric knob here already does."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    run = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env={**clean, "CHAT_MAX_ROOMS": "lots"},
    )
    assert run.returncode != 0, "app booted with a non-numeric CHAT_MAX_ROOMS"
    assert "ValueError" in run.stderr


def test_the_poll_interval_is_a_knob_and_defaults_where_it_was_hardcoded() -> None:
    """CHAT_WAIT_POLL was the literal 0.5 in app.py. It reaches `app`, which is the module
    the wait loop sleeps on — a knob that stopped at `config` would parse and change
    nothing, exactly like the waiter caps above."""
    values = boot()
    assert values["config.WAIT_POLL"] == values["app.WAIT_POLL"] == 0.5
    moved = boot(CHAT_WAIT_POLL="0.05")
    assert moved["config.WAIT_POLL"] == moved["app.WAIT_POLL"] == 0.05


def test_the_poll_interval_floors_above_zero() -> None:
    """Unlike the waiter caps, 0 is NOT a real setting here: it would drop the loop's sleep
    to nothing and spin, burning a core and issuing unbounded tail reads per waiter. So it
    floors at 0.01 rather than at 0 — a misconfiguration degrades to aggressive polling
    instead of to a way of taking an instance down from the environment."""
    for raw in ("0", "-1", "0.001"):
        assert boot(CHAT_WAIT_POLL=raw)["config.WAIT_POLL"] == 0.01


def test_the_dupe_filter_knobs_default_on_and_reach_the_lanes() -> None:
    """The filter's default is ON at 60s/5 copies - the decision this release made once
    the false-positive shape was measured. And the knobs must reach `app`, which
    is the module the write lanes read them from at call time - a knob that stopped at
    config would parse cleanly and filter nothing."""
    values = boot()
    assert values["config.DUPE_FILTER_SECONDS"] == values["app.DUPE_FILTER_SECONDS"] == 60
    assert values["config.DUPE_MIN_LENGTH"] == values["app.DUPE_MIN_LENGTH"] == 16
    assert values["config.DUPE_MAX_COPIES"] == values["app.DUPE_MAX_COPIES"] == 5
    moved = boot(CHAT_DUPE_FILTER_SECONDS="90", CHAT_DUPE_MIN_LENGTH="24", CHAT_DUPE_MAX_COPIES="7")
    assert moved["config.DUPE_FILTER_SECONDS"] == moved["app.DUPE_FILTER_SECONDS"] == 90
    assert moved["config.DUPE_MIN_LENGTH"] == moved["app.DUPE_MIN_LENGTH"] == 24
    assert moved["config.DUPE_MAX_COPIES"] == moved["app.DUPE_MAX_COPIES"] == 7


def test_the_dupe_filter_knobs_floor_sensibly() -> None:
    """A negative window is 0 (off), not a refusal to boot - an operator backing out of
    the filter by deleting a digit should not take the service down. The copy floor is
    at 1, because 0 would refuse the FIRST copy of everything: that is not a filter, it
    is turning the room off. min_length floors at 0, where it means filter everything -
    a real setting an operator could want."""
    negative = boot(CHAT_DUPE_FILTER_SECONDS="-5")
    assert negative["config.DUPE_FILTER_SECONDS"] == 0
    assert boot(CHAT_DUPE_MIN_LENGTH="-1")["config.DUPE_MIN_LENGTH"] == 0
    assert boot(CHAT_DUPE_MAX_COPIES="0")["config.DUPE_MAX_COPIES"] == 1


def test_junk_in_the_poll_interval_refuses_to_boot() -> None:
    """It goes through `_finite_env` like CHAT_MAX_WAIT: `inf` is the case `float()` would
    otherwise accept happily, and an infinite poll interval means a `?wait=` that sleeps
    past its own deadline and never re-reads the room."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    for raw in ("soon", "inf", "nan"):
        run = subprocess.run(
            [sys.executable, "-c", PROBE],
            capture_output=True,
            text=True,
            env={**clean, "CHAT_WAIT_POLL": raw},
        )
        assert run.returncode != 0, f"CHAT_WAIT_POLL={raw!r} booted"
