"""`ci-not-run`'s lifecycle: which observations may remove the CI-has-not-run notice.

The notice is a single upserted comment, so every bug in it is a bug about *when the
comment is deleted* rather than about what it says. Two of those were found on review, and
both fail silently -- the workflow goes green, and the only symptom is a thread that says
the wrong thing about whether CI ran.

The first is a three-state problem written as a boolean. `ci-not-run` polls for the head's
CI run, and "no run visible yet" is not the same observation as "a run exists and is not
gated": the first is ignorance, the second is evidence. Collapsing them means a synchronize
whose new run has not surfaced within the poll deletes the *previous* head's notice, and
nothing puts it back -- this job fires only on opened/reopened/synchronize, and
`ci-not-run-clear` ignores `action_required` by design.

The second is that every read here is a snapshot. Approving a gated run emits no event this
job can see, so the run can leave `action_required` between the read and the write, and the
job then posts a notice that is already false. Re-reading *before* the write only narrows
that window; the reconcile has to come after the write, because the write is what makes the
stale notice exist.

Both are pinned structurally rather than behaviourally, and that limit is worth stating: no
JavaScript runs in this suite. `tests/package.json` is Playwright for the browser gate and
says so, and `tests/edge/test_edge_worker.py` -- the one other test whose subject is
JavaScript -- also asserts agreement by parsing rather than by executing. An executable
regression would mean a Node runner and a `github-script` shim, which is a larger change
than the workflow it would be testing.

Parsed with a regex rather than a YAML library, for the reason
`tests/unit/test_browser_gate.py` gives: PyYAML is here only as somebody else's transitive
dependency, and these tests should not acquire one.

Run: uv run --group dev python -m pytest tests/unit/test_queue_guard_ci_notice.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "queue-guard.yml"

# The `ci-not-run` job's inline script, up to the next job at the same indentation.
# `ci-not-run-clear` is a separate lifecycle with its own rules and must not be caught here.
_JOB = re.compile(r"^  ci-not-run:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n)", re.M | re.S)
_SCRIPT = re.compile(r"^ *script: \|\n(.*)", re.M | re.S)


def _ci_not_run_script() -> str:
    job = _JOB.search(WORKFLOW.read_text(encoding="utf-8"))
    assert job, "the ci-not-run job is no longer findable in queue-guard.yml"
    script = _SCRIPT.search(job.group(1))
    assert script, "the ci-not-run job no longer has an inline `script:` block"
    return script.group(1)


def test_an_unobserved_run_is_not_evidence_and_never_deletes_the_notice():
    """The regression: `blocked === false` once meant both "observed, not gated" and "not
    observed at all", and both deleted. A PR whose replacement head is still waiting for
    approval then loses the warning at the moment it is most true, and stays silent until
    some other PR action happens to re-run the job."""
    script = _ci_not_run_script()
    assert "if (state === 'unknown') return;" in script, (
        "the unobserved case must return before anything is written or removed; "
        "a boolean that folds it in with 'the run is fine' is the bug this pins"
    )
    guard = script.index("if (state === 'unknown') return;")
    first_delete = script.index("deleteComment")
    assert guard < first_delete, (
        "an unobserved run reaches a deleteComment: the early return must precede "
        "every delete path, or ignorance deletes on the strength of nothing"
    )


def test_the_three_observations_stay_distinct():
    """Naming them is the fix; a later edit that reduces them to two states brings the
    bug back with the tests still passing, because every other assertion here is about
    ordering rather than about how many cases exist."""
    script = _ci_not_run_script()
    for state in ("'unknown'", "'gated'", "'ran'"):
        assert state in script, f"the {state} observation is gone from ci-not-run"


def test_the_gated_path_rechecks_the_run_after_it_writes():
    """Approving a run emits no event this job sees, so the notice can be false the moment
    it is posted. The re-read has to sit after the write: before it, this is still
    check-then-act with a shorter gap, which is the same defect measured differently."""
    script = _ci_not_run_script()
    write = max(script.rindex("createComment"), script.rindex("updateComment"))
    recheck = script.rindex("listWorkflowRunsForRepo")
    assert recheck > write, (
        "the run state is never re-read after the notice is written, so a run approved "
        "during the job leaves a notice saying it was not"
    )
    assert script.rindex("deleteComment") > recheck, (
        "the post-write re-read reaches no delete, so it observes the stale notice "
        "and leaves it in place"
    )
