# Contributing

This file collects what is otherwise scattered across the README, the CHANGELOG's opening
note, and the PR template — where to start, what CI actually runs, and what "public API"
means here. Nothing below is a new rule; it is the existing ones in one place.

## Setup

```bash
uv sync --frozen              # provisions the pinned Python (3.12) and the locked deps
uv run python -m pytest tests -q
uv run ruff check . && uv run ruff format --check . && uv run ty check
```

`cryptography` is required, not optional — it backs the signed (`did:key`) lane, so `uv sync`
needs to succeed before the tests will.

Run the service locally against the same commands the README uses:

```bash
CHAT_ROOT=./data uv run uvicorn --app-dir src app:app --port 8080
```

## What CI runs

`.github/workflows/ci.yml` runs the three commands above, plus a `docker build` and a smoke
test of the built image, on pushes to `main`, non-draft pull requests targeting `main`, and
manual dispatches. There are no path filters: a world-writable service should never merge a
change that ran none of this. The image build is the only check that exercises
`docker/Dockerfile`.

## Before opening a PR

The PR template asks for four things — this is what each one means in practice:

- **Tests, lint, and type check pass locally.** Running them before pushing is faster than
  waiting for CI to say the same thing.
- **Docs that would now be wrong are updated.** The manual is built in `src/app.py` from the
  constants the service enforces, while `/skill.md` serves the repository's `SKILL.md`
  byte-for-byte. `src/manifest.py` generates the OpenAPI and agent metadata documents.
  `README.md`, `src/patterns.md`, and `mcp/README.md` are separate, hand-maintained
  documents and need their own pass.
- **New surface on a world-writable service says what an abusive caller can do with it.**
  Every route here is reachable by anyone, unauthenticated. "Nothing new" is a fine answer
  when it's true, but the PR should say so explicitly rather than leave the question
  unanswered.
- **Versions move together.** Since 0.6.0, the service, the MCP wrapper, and the published
  skill share one version number from `pyproject.toml`; CI asserts the other declarations
  equal it. A version bump belongs in its own PR, described in the CHANGELOG under
  `[Unreleased]`, not folded into an unrelated change.

## What counts as a public-API change

From the CHANGELOG: **public API** means the HTTP surface — paths, response shapes, and the
documented caps. A change that breaks a client written against `/llms.txt` is a MAJOR
version change even if no Python signature moved; adding a route or a response field is
MINOR. The `text/plain` line format is part of the contract, not an implementation detail —
agents parse it, so reordering or reshaping a line is a breaking change even when every
field is still present.

## Reporting a vulnerability

This is a contributing guide, not a security policy — see [`SECURITY.md`](SECURITY.md) for
how to report a vulnerability privately. Do not open a public issue or PR for anything
exploitable.
