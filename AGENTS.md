# AGENTS.md

CI runs exactly these — run them before pushing:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run coverage run -m pytest tests -q
uv run coverage report
```

Layering: the core is `src/store.py`, `src/didkey.py`, `src/config.py`, `src/limit.py`
and a thin `src/app.py` adapter.
`src/manifest.py`, docs and frontends are extra — never counted as core.
Core size caps live in `sz-baseline.json` (`caps`); check with `uv run sz.py --caps`.
Growth past a cap needs a new primitive, or belongs in extra.

- If uv cannot write its default cache (sandboxed agent/CI environments), use
  `UV_CACHE_DIR=$PWD/.uvcache` — a worktree-local cache always works.

- Input handling has one rule, written down once: docs/design.md §3.5 — advisory params
  (`limit`, `wait`, `n`, `format`, `since`) clamp and the schema documents the clamp;
  semantic ones (identity, content, `if=`/`if_absent`, names) refuse with a 400 naming the
  field. `tests/test_contract.py` fails the build on drift either way.
- File against HEAD, verified on a fresh fetch — main moves several times a day, and a
  snapshot review reports findings against code that no longer exists. Name the commit;
  the issue form requires it.
- One issue, one PR: if an open PR already covers it, review or extend that one. CI
  comments on a new PR when open PRs cite the same issue (queue-guard.yml).
- Never edit CHANGELOG.md or sz-baseline.json in an ordinary PR — both are
  maintainer-regenerated. Release-note wording goes in the PR body; if a size cap binds,
  propose the missing primitive or move the change to extra. **A release PR a maintainer
  asked for is the exception**: folding [Unreleased] into a dated section *is* the
  packaging, and release.yml refuses to tag a version CHANGELOG.md has no section for.
  queue-guard only fails fork PRs, so on an in-repo branch this is a rule, not a check.
- Move tests, don't rewrite them: test bodies stay byte-identical across reorganisations.
- Size the core with `uv run sz.py` (table) and `uv run sz.py --check`
  (fails if core code-lines grew past `sz-baseline.json`). Its PEP 723 header makes it
  standalone: `uv run` it from anywhere, no project env needed.
- quint-specs/, quint-oracle.*.json, src/oracle_client.py, tests/conftest.py, preflight.sh,
  submit.sh are local-only WIP — never commit them.
