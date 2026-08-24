"""The MCP wrapper's one dependency is: none.

mcp/pyproject.toml declares no dependencies and CI builds the wheel from source, but
nothing stops a stray third-party import from slipping in — the suite here runs inside the
dev environment, where starlette and friends are installed, so an import of any of them
would keep passing right up until `uv publish` ships a package that breaks on import for
everyone using a bare interpreter. This freezes the property directly: import the package
in a fresh interpreter with nothing but the stdlib preloaded, and require that every
module it pulled in belongs to the standard library or to the package itself.

A fresh interpreter is the point, not a nicety. An in-process `sys.modules` diff is
blind to exactly the regression this guards against: the suite itself loads starlette
(tests/_client.py builds a TestClient), so a wrapper that grew `import starlette`
would find it already resident, the before/after diff would never name it, and both
checks would pass. In the child the only preloaded modules are the interpreter's own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# -I: isolated mode — no PYTHONPATH, no user site, no cwd on sys.path — so the probe's
# only non-stdlib path entry is the mcp/src we hand it, and a third-party import has to
# actually load (and be named in the diff) rather than ride in preloaded.
_PROBE = (
    "import sys, json\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "before = set(sys.modules)\n"
    "import technocore_mcp, technocore_mcp.server\n"
    "print(json.dumps(sorted(set(sys.modules) - before)))\n"
)


def _pulled_in_a_fresh_interpreter() -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(ROOT / "mcp" / "src")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"probe interpreter failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_the_wrapper_imports_only_stdlib_and_itself():
    pulled = _pulled_in_a_fresh_interpreter()
    assert pulled, "the package failed to import at all"
    for name in pulled:
        root = name.split(".")[0]
        assert root in sys.stdlib_module_names or root == "technocore_mcp", (
            f"technocore_mcp now imports {name!r}, which is neither stdlib nor its own "
            "package — mcp/pyproject.toml declares no dependencies",
        )


def test_the_wrapper_pulls_in_no_third_party_transitively():
    # Same probe, asserted at root granularity: a transitive pull is caught however it
    # arrives — including one that loads fine in this venv, which is precisely the case
    # an in-process diff cannot see (see the module docstring).
    pulled = _pulled_in_a_fresh_interpreter()
    roots = {name.split(".")[0] for name in pulled}
    foreign = sorted(roots - {"technocore_mcp"} - set(sys.stdlib_module_names))
    assert not foreign, f"third-party modules imported: {sorted(foreign)}"
