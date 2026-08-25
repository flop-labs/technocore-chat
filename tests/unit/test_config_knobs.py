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
    "'config.MAX_WAITERS_TOTAL': config.MAX_WAITERS_TOTAL, "
    "'limit.MAX_WAITERS_TOTAL': limit.MAX_WAITERS_TOTAL, "
    "'app.MAX_WAITERS_TOTAL': app.MAX_WAITERS_TOTAL, "
    "'config.MAX_WAITERS_PER_IP': config.MAX_WAITERS_PER_IP, "
    "'limit.MAX_WAITERS_PER_IP': limit.MAX_WAITERS_PER_IP, "
    "'app.MAX_WAITERS_PER_IP': app.MAX_WAITERS_PER_IP, "
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
    """An instance that sets nothing does not move. These three numbers were hardcoded
    before 0.9.0, and every deployment already running is that instance."""
    values = boot()
    assert values["config.MAX_ROOMS"] == 5120
    assert values["config.MAX_WAITERS_TOTAL"] == 64
    assert values["config.MAX_WAITERS_PER_IP"] == 4
    assert values["config.WORKERS"] == 1  # no WEB_CONCURRENCY set


def test_the_environment_moves_them_at_every_binding_site() -> None:
    """…and reaches the modules that actually enforce them. store gates room creation on
    its own `MAX_ROOMS`, and app passes its own `MAX_WAITERS_*` into `limit._waiter_slot`,
    so a knob that stopped at `config` would parse cleanly and change nothing."""
    values = boot(
        CHAT_MAX_ROOMS="99",
        CHAT_MAX_WAITERS_TOTAL="7",
        CHAT_MAX_WAITERS_PER_IP="2",
        WEB_CONCURRENCY="3",
    )
    assert values["config.MAX_ROOMS"] == values["store.MAX_ROOMS"] == 99
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
