"""The `python -m technocore_exporter` shim.

The guard is not ceremony. Without it `main()` runs on *import* of this module, so
anything that imports the package's submodules — a docs tool, `pkgutil.walk_packages`, a
coverage or packaging pass — binds a port and never returns. `mcp/` has no `__main__.py`
to copy, so this follows the stdlib convention rather than an in-repo precedent.
"""

from .server import main

if __name__ == "__main__":  # pragma: no cover
    main()
