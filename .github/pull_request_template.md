## What

<!-- One or two sentences. What changed, and what a caller of the service or the MCP
wrapper sees differently. -->

## Why

<!-- The problem, not the patch. Link the issue or PR this follows on from, and name
anything open it subsumes, sits on top of, or deliberately leaves alone. -->

## Checks

- [ ] `uv run coverage run -m pytest tests -q && uv run coverage report`
- [ ] `uv run ruff check . && uv run ruff format --check .` and `uv run ty check`
- [ ] Docs that would now be wrong are updated: the manual and `/skill.md` are built in
      `src/app.py`, plus `README.md`, `src/patterns.md`, `mcp/README.md`
- [ ] New surface on a world-writable service: say what an abusive caller can do with it,
      or say "nothing new"
