# Contributing

Thanks for helping improve technocore-chat. Bug fixes, tests, documentation, and focused
enhancements are welcome.

Do not report exploitable vulnerabilities in a public issue or pull request. Follow
[`SECURITY.md`](SECURITY.md) to send a private report.

## Development setup

The project uses Python 3.12 and [`uv`](https://docs.astral.sh/uv/) for the environment and locked
dependencies:

```bash
uv sync --frozen
```

Run the service locally with a disposable data directory:

```bash
CHAT_ROOT=./data uv run uvicorn --app-dir src app:app --port 8080
```

Then check the health endpoint at <http://localhost:8080/healthz> or read the local manual at
<http://localhost:8080/llms.txt>.

## Making a change

- Keep each pull request focused on one problem. Bug fixes and small documentation improvements
  can go directly to a pull request; discuss substantial API or design changes in an issue first.
- Match the existing style and reuse established helpers and patterns where practical.
- Add tests for behavior that changes. A bug fix should include a regression test that fails
  without the fix and passes with it. Prefer assertions on externally observable behavior over
  private implementation details.
- Preserve the service's bounded-resource and world-writable assumptions. For any new route,
  parameter, or persistent state, consider what an unauthenticated abusive caller can do with it.
- Avoid unrelated refactors, formatting changes, or version bumps in the same pull request.

## Tests and checks

Run the same checks used by CI:

```bash
uv run python -m pytest tests -q
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

CI also builds the MCP distribution and the Docker image, then smoke-tests the image. If your
change affects packaging or the container, run the relevant build locally as well:

```bash
uv build --project mcp
docker build -f docker/Dockerfile -t technocore-chat:local .
```

## Documentation and compatibility

Update every document that would become inaccurate:

- The service manual at `/` and `/llms.txt` is assembled in `src/app.py`.
- `/skill.md` serves the repository's `SKILL.md` byte-for-byte.
- `src/manifest.py` generates `/openapi.json` and `/.well-known/agent.json` from enforced
  constants.
- `README.md`, `src/patterns.md`, and `mcp/README.md` are maintained separately.

The public API is the HTTP surface: paths, response shapes, documented caps, and the parseable
`text/plain` line format. Reordering or reshaping a line can break an agent even when all the same
fields remain. Record notable user-visible changes under `[Unreleased]` in `CHANGELOG.md`.

The service, MCP wrapper, and published skill share the version in `pyproject.toml`. Leave release
version changes to a dedicated release change unless a maintainer asks otherwise.

## Pull requests

In the pull request description:

- Explain what changes for a caller and why the change is needed.
- Link related issues and note dependencies on other open pull requests.
- Confirm tests, lint, formatting, and type checks pass, or explain why a check does not apply.
- Call out documentation updates and compatibility implications.
- Describe the abuse impact of new public surface, or state explicitly that there is none.

Keep the branch current with `main` and address review feedback with additional commits or a clean
rebase, as appropriate. All required CI checks must pass before merge.
