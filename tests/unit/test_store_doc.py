"""docs/store.md is generated, so it cannot drift from src/store.py.

Regenerates the page the same way scripts/gen_store_doc.py does and diffs against the
committed copy: a signature change or a new public function fails here until the doc is
regenerated.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _render() -> str:
    # Loaded by path, the same way the generator runs, so neither type checking nor a
    # bare pytest invocation needs scripts/ on the module search path.
    spec = importlib.util.spec_from_file_location(
        "gen_store_doc", ROOT / "scripts" / "gen_store_doc.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render()


def test_the_store_doc_matches_the_module():
    committed = (ROOT / "docs" / "store.md").read_text(encoding="utf-8")
    assert committed == _render(), (
        "docs/store.md is stale — run: uv run python scripts/gen_store_doc.py"
    )


def test_the_store_doc_covers_every_public_function():
    committed = (ROOT / "docs" / "store.md").read_text(encoding="utf-8")
    import store

    for name, fn in vars(store).items():
        if inspect.isfunction(fn) and fn.__module__ == "store" and not name.startswith("_"):
            assert f"`{name}(" in committed, f"{name} is missing from docs/store.md"
