"""What the MCP wrapper is allowed to import, frozen.

The wrapper used to declare no dependencies at all, and this file froze that. It has one
now — the MCP SDK — which changes the rule but not the reason: the suite runs inside the
dev environment, where pytest, hypothesis, starlette and everything else are installed,
so a stray `import requests` in the wrapper would keep passing right up until `uvx
technocore-mcp` shipped a package that breaks on import for everyone.

The rule is now "nothing beyond what `mcp/pyproject.toml` declares, or what that
declaration in turn declares". Read off the *source*, not off a runtime `sys.modules`
diff: what the wheel promises is a property of the lines this repo writes, and only the
source distinguishes them from the SDK's own transitive graph — which is the SDK's
business and is a moving target no assertion here should own.

The second rule is the Cloudflare one, and it is the reason the first is not enough.
Python Workers run on Pyodide, where `uvicorn` is not installable at all — the SDK marks
it `sys_platform != "emscripten"`, so the Worker's resolution simply will not produce it.
Something in the closure reaches for it anyway (`sse_starlette`, to hook a signal
handler), behind a `try/except ImportError` that makes it optional. "Optional" is a
property that can be lost in a version bump, and losing it would take the remote
deployment down while every other check here stayed green — so the second test asserts it
directly, in a fresh interpreter where `uvicorn` cannot be found at all.

A fresh interpreter is the point of that one, not a nicety. An in-process check is blind
to it: this suite has uvicorn loaded before it starts, so the import would find it
resident and pass while the deployment failed.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "mcp" / "src" / "technocore_mcp"

# The wrapper imports what it declares, and each declared dependency's own direct
# requirements are fair game with it: `pydantic` builds the schemas, `starlette` is the
# type of the app the SDK returns, `anyio` is the concurrency the SDK runs on. Anything
# past that first ring is a transitive graph — not something this wheel's dependency line
# promises a user.
_ROOT_DEPENDENCIES = {"mcp", "cryptography"}

# `import x` where the module and its distribution are not named alike.
_EXTRA_TOP_LEVEL = {"mcp": {"mcp", "mcp_types"}}

# A fresh interpreter where `uvicorn` is unimportable however it is asked for. A
# `sys.meta_path` finder rather than a doctored `sys.path`: it refuses the name at
# resolution time, so a submodule import, a late import and an import from inside a `try`
# all see exactly what Pyodide shows them.
_NO_UVICORN_PROBE = (
    "import sys, json\n"
    "class Absent:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] == 'uvicorn':\n"
    "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
    "        return None\n"
    "sys.meta_path.insert(0, Absent())\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import technocore_mcp, technocore_mcp.server\n"
    "app = technocore_mcp.streamable_http_app()\n"
    "print(json.dumps(['uvicorn' in sys.modules, sorted(r.path for r in app.routes)]))\n"
)


def _imported_roots() -> dict[str, str]:
    """Every top-level module name the wrapper's own source imports, and where from.

    The whole tree, at every depth: a function-scope import ships in the wheel exactly
    like a module-scope one and fails for the user at exactly the same moment.
    """
    found: dict[str, str] = {}
    for source in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], source.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.setdefault(node.module.split(".")[0], source.name)
    return found


def _allowed_roots() -> set[str]:
    """Top-level module names the wheel's dependency line accounts for."""
    declared = tomllib.loads((ROOT / "mcp" / "pyproject.toml").read_text())
    names = {
        requirement.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
        for requirement in declared["project"]["dependencies"]
    }
    assert names == _ROOT_DEPENDENCIES, f"unreviewed wrapper dependencies: {names}"

    owners = packages_distributions()
    by_distribution: dict[str, set[str]] = {}
    for module, distributions in owners.items():
        for name in distributions:
            by_distribution.setdefault(name.lower().replace("_", "-"), set()).add(module)

    allowed = {"technocore_mcp"}
    for root in _ROOT_DEPENDENCIES:
        for name in [root, *_direct_requirements(root)]:
            key = name.lower().replace("_", "-")
            allowed |= by_distribution.get(key, set())
            allowed |= _EXTRA_TOP_LEVEL.get(key, set())
    return allowed


def _direct_requirements(name: str) -> set[str]:
    try:
        requires = distribution(name).requires or []
    except PackageNotFoundError:  # pragma: no cover - the dev env installs the SDK
        return set()
    return {
        requirement.split(";")[0].split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
        for requirement in requires
    }


def test_the_wrapper_imports_only_stdlib_itself_and_what_the_wheel_declares():
    allowed = _allowed_roots()
    for name, source in sorted(_imported_roots().items()):
        assert name in sys.stdlib_module_names or name in allowed, (
            f"{source} imports {name!r}, which is neither the standard library, this "
            "package, nor anything mcp/pyproject.toml's `mcp` dependency accounts for"
        )


def test_the_wrapper_builds_its_remote_app_with_no_uvicorn_installed():
    """The Cloudflare guard. Everything the Worker actually runs — importing the package,
    registering the tools, building the streamable-HTTP app — has to work on a runtime
    where the ASGI *server* does not exist, because there the platform is the server."""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _NO_UVICORN_PROBE, str(ROOT / "mcp" / "src")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"the wrapper needs uvicorn to import:\n{proc.stderr}"
    loaded, routes = json.loads(proc.stdout)
    assert loaded is False
    assert routes == ["/mcp"]
