"""The MCP wrapper's one dependency is: none.

mcp/pyproject.toml declares no dependencies and CI builds the wheel from source, but
nothing stops a stray third-party import from slipping in — the suite here runs inside the
dev environment, where starlette and friends are installed, so an import of any of them
would keep passing right up until uv publish ships a package that breaks on import for
everyone using a bare interpreter. This freezes the property directly: import both modules
and require that every module they pulled in belongs to the standard library or to the
package itself.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp" / "src"))

before = set(sys.modules)


def _pulled_by(module: str) -> list[str]:
    importlib.import_module(module)
    return sorted(set(sys.modules) - before)


def test_the_wrapper_imports_only_stdlib_and_itself():
    pulled = _pulled_by("technocore_mcp")
    assert pulled, "the package failed to import at all"
    for name in pulled:
        root = name.split(".")[0]
        assert root in sys.stdlib_module_names or root == "technocore_mcp", (
            f"technocore_mcp now imports {name!r}, which is neither stdlib nor its own "
            "package — mcp/pyproject.toml declares no dependencies",
        )


def test_the_wrapper_pulls_in_no_third_party_transitively():
    # The first test covers names the import brought in; this one watches the interpreter
    # state, so a transitive pull is caught even when it hides under an allowed root.
    import technocore_mcp.protocol  # noqa: F401
    import technocore_mcp.server  # noqa: F401

    roots = {name.split(".")[0] for name in set(sys.modules) - before}
    foreign = sorted(roots - {"technocore_mcp"} - set(sys.stdlib_module_names))
    assert not foreign, f"third-party modules imported: {sorted(foreign)}"
