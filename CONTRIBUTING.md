# Contributing

Thanks for helping improve technocore-chat. Bug fixes, tests, documentation, and focused
enhancements are welcome.

Do not report exploitable vulnerabilities in a public issue or pull request. Follow
[`SECURITY.md`](SECURITY.md) to send a private report.

## Development setup

Python 3.12, with [`uv`](https://docs.astral.sh/uv/) for the environment and locked dependencies.
`just` is one of those dependencies, so the sync is the whole setup:

```bash
uv sync --frozen
uv run just
uv run just serve   # the service, on a disposable ./data directory
```

`uv run just` lists the recipes; each command's reasoning sits beside it in the `justfile`. Then
check <http://localhost:8080/healthz>, or read the local manual at <http://localhost:8080/llms.txt>.

## Making a change

- Keep each pull request focused on one problem. Bug fixes and small documentation improvements
  can go directly to a pull request; discuss substantial API or design changes in an issue first.
- Fix where the invariant lives. A defect reported on one lane is rarely one lane's defect:
  patch the layer that decides the value, not the one that surfaced it. A fix at the display
  layer leaves every other consumer wrong while reading as fixed.
- Name the lanes that share a defect, and either fix them here or say why not. A partial fix
  is often the right scope; a silent one is not.
- One grammar, one parser. Extend the regex, schema or helper that already owns a shape rather
  than adding a second check beside it — two validators for one grammar stop agreeing, and the
  stale one is the one nobody remembers is there.
- Match the existing style and reuse established helpers and patterns where practical.
- Add tests for behavior that changes. A bug fix should include a regression test that fails
  without the fix and passes with it. Prefer assertions on externally observable behavior over
  private implementation details.
- Lifecycle behavior — append, read, expiry, compaction, the reaper, conditional writes — is also
  covered by a Hypothesis state machine in `tests/test_store_stateful.py`. If a change alters one
  of those promises, put the promise there too: the bugs that survive example tests are the ones
  needing a particular *sequence*.
- Preserve the service's bounded-resource and world-writable assumptions. For any new route,
  parameter, or persistent state, consider what an unauthenticated abusive caller can do with it.
- Avoid unrelated refactors, formatting changes, or version bumps in the same pull request.
- No code golf. A low core line count is a constraint, not a score — unreadability is a
  reject even when the line count goes down.
- Comments carry why, not what. This codebase comments heavily on purpose: the reasoning
  outlives the line. A comment that restates the code is noise and goes stale silently; one
  that names the failure it prevents, or the case it deliberately does not cover, earns its
  place. Length is not the measure.
- Line tradeoffs: three lines over a useful primitive is an easy yes; three hundred lines
  means either a new primitive is missing or the change belongs in extra, not core. The
  numeric form is part of `just check` — the per-file caps in `sz-baseline.json`.
- Benchmark claimed speedups against `tests/capacity_bench.py` — a number, not a hunch. That is
  `just bench`; a `perf` pull request also gets base-versus-head numbers from CI (see below).
- Removing dead code from core is a win on its own; open a pull request for it.

## Overlapping work

Several pull requests racing one issue cost more review than they save. Part of that is automated
in `.github/workflows/queue-guard.yml`: one comment listing open PRs that cite the same issues, and
a failure on a *fork* PR touching `CHANGELOG.md` or `sz-baseline.json`. Core size is gated from
both ends: `just check` enforces the immutable policy ceilings, which a fork PR can satisfy without
editing that protected baseline, and the ratchet against it runs on pushes to `main`, where
maintainers can regenerate it. Do not read a green check as permission. What only you can do:

- Verify claims against current `main`, not a cached copy of the source, and name the commit.
- If an open PR already addresses the issue, review or build on it — with credit — rather than
  filing a competitor. If yours is materially different, say what the earlier one does not do
  and link it. Collisions that are not clear-cut are the maintainers' call, not a reason for
  mutual stand-down.

## Tests and checks

```bash
uv run just check
```

CI's lint + tests job runs that same recipe, so the checks you run and the checks CI gates on
cannot drift. `uv run just build` is the packaging half: the MCP distribution and the image.

### The contract check

Every pull request fuzzes the running service against the `/openapi.json` that same instance
serves. **An undocumented status code fails it**, so a new route or response goes into
`src/manifest.py` in the same change.

```bash
uv run just contract
```

### Mutation testing

Weekly, never on a pull request (`.github/workflows/mutation.yml`), over the code where being wrong
is silent: TTL thresholds, the authorization gates, the caps, the refusal bodies — the scope, and
the reasoning beside each entry, is `tests/mutation_scope.py`. A surviving mutant is a question,
not a failure: it means the suite would not have noticed that change.

```bash
uv run just mutate
uv run just mutate-patterns   # what a full run covers, in seconds rather than hours
```

## Documentation and compatibility

Update every document that would become inaccurate:

- The service manual at `/` and `/llms.txt` is assembled in `src/app.py`.
- `/skill.md` serves the repository's `SKILL.md` byte-for-byte.
- `src/manifest.py` generates `/openapi.json` and `/.well-known/agent.json` from enforced constants.
- `README.md`, `src/patterns.md`, and `mcp/README.md` are maintained separately.

The public API is the HTTP surface: paths, response shapes, documented caps, and the parseable
`text/plain` line format. Reordering or reshaping a line can break an agent even when all the same
fields remain. Maintainers regenerate `CHANGELOG.md` and `sz-baseline.json` and queue-guard fails a
fork PR that edits either, so describe notable user-visible changes in the pull request body.

### Translations of agent-facing documents

The documents an agent reads — `/llms.txt`, `/skill.md`, `/patterns.md`, `/interop.md`, `/auth.md`,
the refusal bodies — are English-only, and a pull request that adds a translated copy of one is
declined. This is about instructions written *for agents*, not about people: open issues, review and
discuss in whatever language you think in. Publish a translation in your own repository, naming the
upstream commit it was built from. If translating showed you something the English document gets
wrong or leaves unsaid, that is a bug in the English document: send that. The reasoning, and the
measurement that would change this policy, are in [`docs/translations.md`](docs/translations.md).

## Pull requests

Fill in the pull request template (`.github/pull_request_template.md`). Three guards in
`.github/workflows/pr-guards.yml` are mechanical:

- The title follows the conventional-commit form the history already uses: `fix:`, `fix(scope):`,
  and the same for `feat`, `docs`, `perf`, `test`, `build`, `ci`.
- A `fix` pull request has to change something under `tests/`, and CI runs those tests against the
  base commit and requires them to fail there: a regression test that passes without the fix is not
  one.
- A `perf` pull request gets `tests/capacity_bench.py` run on base and on head, with both numbers
  posted to the job summary.

Keep the branch current with `main`, address review feedback with additional commits or a clean
rebase, and expect every required check to pass before merge.
