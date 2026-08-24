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

Layering: the core is `src/store.py`, `src/didkey.py`, `src/config.py` and a thin
`src/app.py` adapter.
`src/manifest.py`, docs and frontends are extra — never counted as core.

- Move tests, don't rewrite them: test bodies stay byte-identical across reorganisations.
- Size the core with `uv run sz.py` (table) and `uv run sz.py --check`
  (fails if core code-lines grew past `sz-baseline.json`). Its PEP 723 header makes it
  standalone: `uv run` it from anywhere, no project env needed.
- quint-specs/, quint-oracle.*.json, src/oracle_client.py, tests/conftest.py, preflight.sh,
  submit.sh are local-only WIP — never commit them.
