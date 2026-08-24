"""docs/store.md is generated, so it cannot drift from src/store.py.

Regenerates the page the same way scripts/gen_store_doc.py does and diffs against the
committed copy: a signature change or a new public function fails here until the doc is
regenerated.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from gen_store_doc import render  # noqa: E402


def test_the_store_doc_matches_the_module():
    committed = (ROOT / "docs" / "store.md").read_text(encoding="utf-8")
    assert committed == render(), (
        "docs/store.md is stale — run: uv run python scripts/gen_store_doc.py"
    )


def test_the_store_doc_covers_every_public_function():
    committed = (ROOT / "docs" / "store.md").read_text(encoding="utf-8")
    import store  # noqa: F401  (registered on the path by gen_store_doc)

    for name, fn in vars(store).items():
        import inspect

        if inspect.isfunction(fn) and fn.__module__ == "store" and not name.startswith("_"):
            assert f"`{name}(" in committed, f"{name} is missing from docs/store.md"
