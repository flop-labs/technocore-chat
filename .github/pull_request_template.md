<!-- Keep the whole description short — aim for under 200 words, and never more than a
screen. Reviewers read the diff; this only has to tell them what to look for and why. Cut
anything the diff already says: no file-by-file walkthrough, no restating the checklist, no
recap of what you tried on the way. Detail that is worth keeping goes in a code comment,
where it stays next to the thing it explains. -->

## What

<!-- One or two sentences. What changed, and what a caller of the service or the MCP
wrapper sees differently. -->

## Why

<!-- The problem, not the patch. Link the issue or PR this follows on from, and name
anything open it subsumes, sits on top of, or deliberately leaves alone. A judgment call a
reviewer might have made differently is worth one line; the reasoning behind it belongs in
the code. -->

## Checks

- [ ] `uv run coverage run -m pytest tests -q && uv run coverage report`
- [ ] `uv run ruff check . && uv run ruff format --check .` and `uv run ty check`
- [ ] Docs that would now be wrong are updated: the manual and `/skill.md` are built in
      `src/app.py`, plus `README.md`, `src/patterns.md`, `mcp/README.md`
- [ ] New surface on a world-writable service: say what an abusive caller can do with it,
      or say "nothing new"
