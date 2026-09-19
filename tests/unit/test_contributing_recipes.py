"""Keep CONTRIBUTING.md's shell blocks pointing at things that exist.

A copied command block rots silently: the dependency group gets folded into another one, a
recipe is renamed, and the document still reads as correct until a new contributor pastes it
and gets an error nobody on the project has seen. This test is the gate that stops that — it
resolves every name the document spends against the file that defines it, so a rename has to
update the prose in the same change or CI says which line went stale.

Fast and offline on purpose: it reads two files and asks `just` to list its own recipes. It
never starts the service and never installs anything, so it belongs in the unit shard.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
JUSTFILE = ROOT / "justfile"
PYPROJECT = ROOT / "pyproject.toml"


def bash_lines(markdown: str) -> list[tuple[int, str]]:
    """Every line inside a ```bash fence, with its 1-based line number in the document.

    Only ```bash blocks: the document also fences prose-ish examples in other languages, and
    a name inside one of those is not a command anybody is going to paste.
    """
    out: list[tuple[int, str]] = []
    inside = False
    for number, line in enumerate(markdown.splitlines(), start=1):
        marker = line.strip()
        if marker.startswith("```"):
            inside = False if inside else marker == "```bash"
            continue
        if inside:
            out.append((number, line))
    return out


def commands(markdown: str) -> list[tuple[int, str]]:
    """The bash lines joined across trailing backslashes, keyed by their first line number.

    Continuations matter: the contract block spells one invocation over four lines, and a
    flag on the second of them is still a flag on that command.
    """
    out: list[tuple[int, str]] = []
    start: int | None = None
    parts: list[str] = []
    for number, line in bash_lines(markdown):
        text = line.strip()
        if start is None:
            start = number
        if text.endswith("\\"):
            parts.append(text[:-1].strip())
            continue
        parts.append(text)
        out.append((start, " ".join(p for p in parts if p)))
        start, parts = None, []
    if start is not None and parts:
        out.append((start, " ".join(p for p in parts if p)))
    return out


def tokens(command: str) -> list[str]:
    """shlex where it parses, whitespace where it does not — a placeholder like
    `<mutant-name>` is fine, but an unbalanced quote in prose should not fail the run."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def dependency_groups(pyproject: str) -> set[str]:
    return set(tomllib.loads(pyproject).get("dependency-groups", {}))


def recipe_names(justfile: Path) -> set[str]:
    """Ask just, so this agrees with whatever `just` itself would accept.

    `just` is a locked dev dependency (rust-just), so it is on PATH under `uv run pytest`.
    The fallback is for a bare `pytest` outside the environment: recipe headers start at
    column zero and are the only such lines that are neither a comment, a `set`, nor an
    assignment. Private recipes (leading underscore) are omitted either way — `--summary`
    hides them, and nothing should document one.
    """
    just = shutil.which("just")
    if just is not None:
        listed = subprocess.run(
            [
                just,
                "--justfile",
                str(justfile),
                "--working-directory",
                str(justfile.parent),
                "--summary",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return set(listed.stdout.split())
    # `[^:]*`, not `[^:=]*`: a parameter default (`serve port="8080":`) puts an `=` before
    # the colon, and excluding it classified every such recipe as an assignment. The lookahead
    # still rejects `name := value`, where the `=` follows the colon.
    header = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)[^:]*:(?!=)")
    names = set()
    for line in justfile.read_text(encoding="utf-8").splitlines():
        if line[:1].strip() and not line.startswith(("#", "set ", "export ")):
            match = header.match(line)
            if match:
                names.add(match.group(1))
    return names


def stale_references(markdown: str, groups: set[str], recipes: set[str]) -> list[str]:
    """One human-readable complaint per name the document spends that nothing defines."""
    complaints: list[str] = []
    for number, command in commands(markdown):
        if re.search(r"\buv\s+sync\b", command):
            for named in re.findall(r"--group[=\s]+(\S+)", command):
                if named not in groups:
                    complaints.append(
                        f"CONTRIBUTING.md line {number}: `uv sync ... --group {named}` names a "
                        f"dependency group pyproject.toml does not define. Defined groups: "
                        f"{', '.join(sorted(groups)) or '(none)'}."
                    )
        argv = tokens(command)
        for index, token in enumerate(argv):
            if token != "just":
                continue
            rest = [word for word in argv[index + 1 :] if not word.startswith("-")]
            if not rest:
                continue  # `just --list` and friends name no recipe.
            if rest[0] not in recipes:
                complaints.append(
                    f"CONTRIBUTING.md line {number}: `just {rest[0]}` is not a recipe in the "
                    f"justfile. Recipes: {', '.join(sorted(recipes)) or '(none)'}."
                )
    return complaints


def test_contributing_names_only_things_that_exist():
    """The gate itself: every group and recipe the document tells you to run resolves."""
    complaints = stale_references(
        CONTRIBUTING.read_text(encoding="utf-8"),
        dependency_groups(PYPROJECT.read_text(encoding="utf-8")),
        recipe_names(JUSTFILE),
    )
    assert not complaints, "\n" + "\n".join(complaints)


def test_the_fallback_parser_keeps_recipes_that_take_default_arguments(tmp_path, monkeypatch):
    """Without `just` on PATH — a bare pytest outside `uv run` — the fallback text parse ran.

    It classified `serve port="8080":` as an assignment because of the `=` in the default,
    so CONTRIBUTING's `just serve` and `just mutate` read as stale and the suite failed on a
    tree where `uv run just serve` works fine. The parse has to agree with `just` itself on
    parameterized recipes, or the test only passes for people who happen to have `just`
    resolvable from the OS PATH.
    """
    justfile = tmp_path / "justfile"
    justfile.write_text(
        "plain:\n    @echo hi\n\n"
        'serve port="8080":\n    @echo {{ port }}\n\n'
        "variadic *args:\n    @echo {{ args }}\n\n"
        'answer := "42"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert recipe_names(justfile) == {"plain", "serve", "variadic"}


def test_the_document_still_has_command_blocks():
    """Guards the extractor, not the document: a fence syntax this stops matching would make
    every assertion above pass by finding nothing to check."""
    found = commands(CONTRIBUTING.read_text(encoding="utf-8"))
    assert len(found) >= 5, f"only {len(found)} commands found in ```bash blocks — parser slip?"
    assert any("uv sync" in command for _, command in found)


GOOD = """
# Setup

```bash
uv sync --frozen --group mutation
uv run just check
just contract
```

```python
just nonsense_in_a_python_block
```

Prose naming `just nonsense_in_prose` outside any fence.
"""

BAD = """
```bash
uv sync --frozen --group nosuchgroup
uv run just nosuchrecipe
```
"""


def test_fixture_with_correct_names_passes():
    assert stale_references(GOOD, {"dev", "mutation"}, {"check", "contract"}) == []


def test_fixture_with_wrong_names_fails_and_says_where():
    complaints = stale_references(BAD, {"dev", "mutation"}, {"check", "contract"})
    assert len(complaints) == 2, complaints
    assert "line 3" in complaints[0] and "nosuchgroup" in complaints[0]
    assert "line 4" in complaints[1] and "nosuchrecipe" in complaints[1]


def test_continuation_lines_are_part_of_their_command():
    split_over_lines = """
```bash
uv sync --frozen \\
  --group nosuchgroup
```
"""
    complaints = stale_references(split_over_lines, {"dev"}, set())
    assert len(complaints) == 1, complaints
    assert "line 3" in complaints[0] and "nosuchgroup" in complaints[0]
