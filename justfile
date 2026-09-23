# The commands this repository is developed with, in one place, so CONTRIBUTING.md can name a
# recipe instead of pasting a shell block that rots the next time CI changes.
# `.github/workflows/ci.yml` runs `just check`, so "CI runs the same checks you do" is a fact
# rather than an aspiration — the two cannot drift, because there is only one list.
#
# Everything goes through `uv run`, and `just` is itself a locked dev dependency (rust-just on
# PyPI), so `uv sync --frozen` stays the whole setup story: no second installer to keep in step
# on a laptop, in CI, or in a container.

# Fail fast and loudly. -e stops a recipe at the first non-zero command instead of running the
# rest against a broken tree, -u turns a typo'd variable into an error rather than an empty
# string, and pipefail keeps `a | b` from reporting green because only the last stage exited 0.
set shell := ["bash", "-euo", "pipefail", "-c"]

# Comments in a recipe body are notes to the next reader, not output: without this they are
# handed to the shell and echoed into the middle of a run.
set ignore-comments := true

# `just` with no arguments lists the recipes rather than running the first one, which would be
# a surprising way to start a full test run.
_default:
    @{{ just_executable() }} --list

# Everything CI gates a change on. Run this before opening a pull request.
check: lint
    # The numeric side of CONTRIBUTING's "no code golf" rule. --caps enforces the immutable
    # per-file policy ceilings, which is the half a fork PR can satisfy on its own: the
    # ratchet in sz-baseline.json is maintainer-generated and protected by queue-guard, so
    # `sz.py --check` stays on pushes to main where the baseline can be regenerated.
    uv run sz.py --caps
    # Branch coverage with the floor in [tool.coverage.report], not statement coverage: the
    # edges that matter in this service are refusals and recovery paths, and a happy-path-only
    # suite can lift a statement percentage while leaving every one of them dark.
    uv run coverage run -m pytest tests -q
    uv run coverage report

# Lint, formatting and types — the static half of `check`, cheap enough to run on save.
lint:
    uv run ruff check .
    # --check, not a rewrite: a gate reports, it does not repair. `just fmt` repairs.
    uv run ruff format --check .
    uv run ty check

# Apply the formatting `lint` only reports on.
fmt:
    uv run ruff format .

# The suite without the coverage harness — the fast loop while writing a test.
test *args:
    # Arguments pass straight through, so `just test tests/unit -k didkey` narrows the run.
    uv run pytest tests -q {{ args }}

# Fuzz the running service against the /openapi.json that same instance serves.
contract:
    #!/usr/bin/env bash
    # A shebang recipe because the server and the trap that stops it have to share one shell;
    # with a line-per-shell body a failed run would leave uvicorn holding port 8099.
    set -euo pipefail
    root="$(mktemp -d)"
    # Tuned so the fuzzer spends its budget on handlers rather than on the parts that exist to
    # stop it. CHAT_MAX_WAIT is the ceiling `wait` clamps to, and at the default 10 a few
    # generated long polls cost more than the whole run; without the rate bumps the limiter
    # answers most of it with a 429 that conforms and checks nothing. Everything else is the
    # shipped configuration.
    CHAT_ROOT="$root" \
    CHAT_MAX_WAIT=1 \
    CHAT_RATE_READ=1000000 CHAT_RATE_WRITE=1000000 CHAT_RATE_ROOMS_PER_DAY=1000000 \
      uv run uvicorn --app-dir src app:app --port 8099 --log-level warning &
    server=$!
    # On EXIT rather than after schemathesis: a failing contract check is precisely the run
    # that would otherwise skip its cleanup and leave the port busy for the next attempt.
    trap 'kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true; rm -rf "$root"' EXIT
    for _ in $(seq 30); do
      curl -fsS localhost:8099/healthz >/dev/null && break
      sleep 1
    done
    # Fail here rather than let schemathesis report a connection error as a contract finding.
    curl -fsS localhost:8099/healthz >/dev/null || {
      echo "the service never answered /healthz on 8099 — nothing was checked" >&2; exit 1; }
    # Deterministic: --generation-deterministic fixes the generation (superseding --seed, which
    # the CLI then ignores) and the exact schemathesis pin in pyproject.toml fixes the
    # generator, so a failure is a change in this service. ~800 requests in under ten seconds.
    #
    # Checks are named rather than `all`, because two of the rest assume things this service
    # breaks on purpose: positive_data_acceptance expects valid input to succeed, but a valid
    # write to a mailbox or a reserved namespace is refused; and negative_data_rejection's
    # generator also invents a query parameter the document does not list and expects a
    # refusal, which this service will never give — ignoring unknown query parameters is what
    # makes the documented `?n=` cache-buster work at all. Turning that one mutation off is a
    # config-file setting the CLI has no flag for, so the negative half of the contract is
    # enforced by tests/test_contract.py (in-process, on every `just test`) instead. What this
    # recipe asserts is the over-the-wire half: no 500s, every response matching the status,
    # content type, headers and schema promised, and the two 405 checks, since Allow is the
    # machine-readable half of a refused method.
    #
    # --warnings lists what to *enable*. Three are left out because they can never clear:
    # missing_auth (POST /r/events only ever 403s, and there are no credentials to suggest),
    # validation_mismatch (a generated 86-character string is never a valid Ed25519 signature),
    # missing_test_data (a generated note name has never been written, and 404 is the right
    # answer). A warning that is always there is one nobody reads.
    uv run schemathesis run http://localhost:8099/openapi.json \
      --url http://localhost:8099 \
      --checks not_a_server_error,status_code_conformance,content_type_conformance,response_headers_conformance,response_schema_conformance,unsupported_method,allow_header_conformance \
      --phases examples,coverage,fuzzing \
      --max-examples 25 \
      --generation-deterministic \
      --max-response-time 10 \
      --warnings missing_deserializer,unused_openapi_auth,unsupported_regex,method_not_allowed,constants_extraction,unmatched_filter \
      --no-color

# Mutation testing over the code where being wrong is silent. Hours, not minutes.
mutate children="4":
    # Weekly in CI, never on a pull request (.github/workflows/mutation.yml), over the TTL
    # thresholds, authorization gates, caps and refusal bodies. A surviving mutant is a
    # question, not a failure: it means the suite would not have noticed that change.
    #
    # `--group mutation` per command rather than a `uv sync --group mutation` first: mutmut is
    # the one tool the ordinary `uv sync --frozen` deliberately leaves out, and asking for the
    # group here keeps this recipe from reshaping the environment every other recipe shares.
    #
    # `xargs` rather than a literal pattern list: the scope lives in tests/mutation_scope.py
    # with the reasoning beside each entry, and a second copy here would drift from it.
    uv run --group mutation python tests/mutation_scope.py --patterns \
      | xargs uv run --group mutation mutmut run --max-children {{ children }}
    # Reported even after a run that died halfway — it still knows what it killed.
    # `uv run --group mutation mutmut show <mutant-name>` is the diff behind one survivor.
    uv run --group mutation mutmut export-cicd-stats
    uv run --group mutation python tests/mutation_scope.py --report

# What `just mutate` would cover, without running it — seconds instead of hours.
mutate-patterns:
    # Also the check that an edit to tests/mutation_scope.py still resolves to real functions.
    uv run --group mutation python tests/mutation_scope.py --patterns

# The capacity numbers behind a claimed speedup — CONTRIBUTING asks for a number, not a hunch.
bench *args:
    # The defaults build the full documented caps, which is the point of the measurement;
    # `just bench --scale 0.25` is the quick pass while iterating.
    uv run python tests/capacity_bench.py {{ args }}

# The service locally on a disposable data directory.
serve port="8080":
    # Then http://localhost:8080/healthz, or the local manual at /llms.txt. Delete ./data to
    # reset the instance — nothing in it is meant to survive.
    CHAT_ROOT=./data uv run uvicorn --app-dir src app:app --port {{ port }}

# The packaging half of CI: the MCP distribution and the image.
build:
    # The wrapper is a separate distribution with its own dependency line, so nothing would
    # notice a broken mcp/pyproject.toml until someone ran `uv publish`; the Dockerfile has no
    # gate at all outside CI. Needs a working docker daemon for the second line.
    uv build --project mcp
    docker build -f docker/Dockerfile -t technocore-chat:local .
