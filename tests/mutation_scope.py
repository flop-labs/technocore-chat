"""What the periodic mutation run covers, and how it reports.

Run: uv run --group mutation python tests/mutation_scope.py --patterns
     uv run --group mutation python tests/mutation_scope.py --report

Mutation testing asks what coverage cannot: not "did a test execute this line" but "would
a test have *noticed* if this line were wrong".

It is scoped, and not run on pull requests. `src/` carries ~3600 mutants; a full pass is
tens of minutes and most of it is noise — a mutated log string is not a defect anyone will
ship. What earns the machine time is code where being wrong is silent:

  ttl            An off-by-one in an idle threshold does not fail. It keeps data a week too
                 long or deletes it a day too early, and nobody finds out from a stack
                 trace. The whole retention promise is four comparisons.
  authorization  Every gate fails closed by design; a mutant that flips one to fail-open is
                 invisible on the happy path. A signed lane that verifies nothing still
                 returns 200.
  caps           The only thing between an anonymous, world-writable service and its disk.
                 A cap with the wrong operator holds right up to the moment it matters.
  guidance       The refusal bodies are the real documentation for an agent that already
                 got something wrong. A test asserting only the status code lets them rot.

Patterns are mutmut mutant names: `<module>.x_<function>__mutmut_<n>`, where a private
`_reap` becomes `x__reap`. Grouped by theme rather than module — `caps` spans three files,
and "is the rate limiter covered" should not require knowing which one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCOPE: dict[str, tuple[str, ...]] = {
    "ttl": (
        "store.x__cutoff__*",  # what "expired" means for an e- room
        "store.x__expired__*",  # …and the per-record comparison behind it
        "store.x__reapable__*",  # the idle and stillborn thresholds
        "store.x__stillborn__*",  # "one message and nobody answered"
        "store.x__guards_a_live_room__*",  # a guard note outlives the idle rule
        "store.x__reap__*",  # the pass itself, including the recheck under the lock
        "store.x__compact__*",  # the ring, and the expiry that rides it
    ),
    "authorization": (
        "app.x__room_write_gate__*",  # mailboxes, owned rooms, allow-lists
        "app.x__note_write_gate__*",  # the two ownership namespaces
        "app.x__signer__*",  # nonce shape, then the signature
        "app.x__burn_nonce__*",  # single-use, by compare-and-set
        "app.x__allowed_keys__*",
        "app.x__reject_if_events_room__*",  # the server-written discovery log
        "store.x__last_nonce__*",  # replay protection inside the room's tail
        "didkey.x_public_key__*",  # the parse that decides what a did:key even is
        "didkey.x_is_did__*",
        "didkey.x_verify__*",  # fails closed or it means nothing
    ),
    "caps": (
        "store.x__check_room_capacity__*",
        "store.x__check_note_capacity__*",
        "store.x__check_capacity__*",
        "store.x__ring_limit__*",  # the full ring, or the floor under pressure
        "store.x_room_bytes_used__*",
        "store.x_clean_text__*",  # the character caps, and the invisible-character sweep
        "limit.x_take__*",  # the rate limiter (src/limit.py since the extraction)
        "limit.x_refund__*",
        "limit.x_dupe_refused__*",  # the cross-sender ring: count, refuse, and the bound
        "limit.x_dupe_release__*",  # and giving a copy back when the append refused it
        "limit.x_normalize_text__*",  # the ladder duplicates are keyed on
        "app.x__room_create_gate__*",  # new rooms per IP per day
        "app.x_read_json__*",  # the body cap, on both the header and the stream
    ),
    "guidance": (
        "app.x_on_not_found__*",  # the route map a wrong URL gets back
        "app.x_on_method_not_allowed__*",
        "app.x_allowed_methods__*",  # …and the Allow header it has to carry
        "app.x_on_bad_input__*",
        "app.x_on_conflict__*",  # the current value, and what to do with it
        "limit.x_limited__*",  # the bucket, the refill rate, the retry delay
        "limit.x_budget_note__*",
    ),
}

# The run failing to do its job, as opposed to finding something. A survivor is a question
# for a human; these are a broken harness, and only the second is worth a red run.
BROKEN = ("suspicious", "segfault", "no_tests", "check_was_interrupted_by_user")

STATS = Path("mutants/mutmut-cicd-stats.json")


def patterns() -> list[str]:
    return [pattern for group in SCOPE.values() for pattern in group]


def _survivors() -> list[str] | None:
    """The scoped mutants no test noticed, or None if `mutmut results` could not be read.

    None rather than an empty list: "nothing survived" and "the tool that lists survivors
    did not run" mean opposite things and must never render the same.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return sorted(
        line.strip().split(":")[0]
        for line in result.stdout.splitlines()
        if line.strip().endswith(": survived")
    )


def report() -> int:
    """Render the run as markdown and return the exit code the job should use."""
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    checked = stats["killed"] + stats["survived"]
    score = (100 * stats["killed"] / checked) if checked else 0.0
    broken = {name: stats[name] for name in BROKEN if stats.get(name)}
    survivors = _survivors()

    # Rendered first because it can add to `broken`, then spliced in below it.
    if not stats["survived"]:
        body = ["Nothing survived: every mutant in scope was caught by a test.", ""]
    elif survivors is None:
        broken["survivors_unreadable"] = stats["survived"]
        body = [
            f"{stats['survived']} mutant(s) survived and `mutmut results` could not be "
            "read, so this cannot say which. Re-run it from the run's working directory.",
            "",
        ]
    else:
        body = [
            "### Survivors",
            "",
            "Each is a change the suite would not have noticed. Some are untestable — an "
            "equivalent mutant, a boundary no caller can reach — and the rest are a "
            "missing test. `mutmut show <name>` prints the diff.",
            "",
            "```",
            *survivors,
            "```",
        ]

    out = [
        "## Scoped mutation run",
        "",
        f"**{stats['killed']} killed, {stats['survived']} survived** "
        f"of {checked} mutants checked — {score:.0f}% caught.",
        "",
        "Scope: " + ", ".join(f"`{theme}`" for theme in SCOPE),
        "",
    ]
    if stats.get("timeout"):
        out += [
            f"{stats['timeout']} mutant(s) timed out — usually a mutated loop bound, and "
            "counted as killed nowhere. Worth a look if the number moves.",
            "",
        ]
    if broken:
        out += [
            "### The run itself is broken",
            "",
            "Not findings: mutants generated and never judged, so the numbers above "
            "understate what is uncovered.",
            "",
            *(f"- `{name}`: {count}" for name, count in broken.items()),
            "",
        ]
    out += body

    print("\n".join(out))
    return 1 if broken else 0


if __name__ == "__main__":
    if "--patterns" in sys.argv:
        print("\n".join(patterns()))
    elif "--report" in sys.argv:
        raise SystemExit(report())
    else:
        raise SystemExit(f"usage: {sys.argv[0]} [--patterns | --report]")
