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
    "'config.ROOMS_CACHE_SECONDS': config.ROOMS_CACHE_SECONDS, "
    "'config.NOTE_STATS_CACHE_SECONDS': config.NOTE_STATS_CACHE_SECONDS, "
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
    assert values["config.ROOMS_CACHE_SECONDS"] == 3.0
    assert values["config.NOTE_STATS_CACHE_SECONDS"] == 30.0
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
        CHAT_ROOMS_CACHE_SECONDS="1.5",
        CHAT_NOTE_STATS_CACHE_SECONDS="2.25",
        WEB_CONCURRENCY="3",
    )
    assert values["config.MAX_ROOMS"] == values["store.MAX_ROOMS"] == 99
    # Its own knob now, so the two move independently — and the per-namespace cap reaches
    # `store`, which is the module `_check_note_capacity` enforces it from.
    assert values["config.MAX_NOTES_PER_NS"] == values["store.MAX_NOTES_PER_NS"] == 400
    for module in ("config", "limit", "app"):
        assert values[f"{module}.MAX_WAITERS_TOTAL"] == 7
        assert values[f"{module}.MAX_WAITERS_PER_IP"] == 2
    assert values["config.ROOMS_CACHE_SECONDS"] == 1.5
    assert values["config.NOTE_STATS_CACHE_SECONDS"] == 2.25
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
    cache_floored = boot(
        CHAT_ROOMS_CACHE_SECONDS="-1", CHAT_NOTE_STATS_CACHE_SECONDS="-1"
    )
    assert cache_floored["config.ROOMS_CACHE_SECONDS"] == 0.0
    assert cache_floored["config.NOTE_STATS_CACHE_SECONDS"] == 0.0
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


def test_cache_windows_refuse_non_finite_values() -> None:
    """A cache window needs an ordering against the clock. Infinity makes the room view's
    recency backstop immortal; NaN makes every comparison false. Both are valid inputs to
    `float()`, so exercise the fresh interpreter that used to accept them silently."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("CHAT_")}
    for name in ("CHAT_ROOMS_CACHE_SECONDS", "CHAT_NOTE_STATS_CACHE_SECONDS"):
        # One value per knob proves the wiring to `_finite_env`; its nan/-inf branches are
        # already pinned directly in test_docs without another full Starlette import.
        run = subprocess.run(
            [sys.executable, "-c", PROBE],
            capture_output=True,
            text=True,
            env={**clean, name: "inf"},
        )
        assert run.returncode != 0, f"app booted with {name}=inf"
        assert f"{name} must be a finite number" in run.stderr
