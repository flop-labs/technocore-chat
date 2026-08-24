#!/usr/bin/env python3
"""Measure the size of the core, and only the core.

Core is src/store.py, src/didkey.py and src/app.py; src/manifest.py is reported under an
"extra" label and never counted in the core total. Two numbers per file:

- code lines: lines carrying executable tokens, with docstrings and comments stripped.
  This is the only number --check guards — the core must not grow.
- comment lines: counted raw. The comments in this repo are protocol rationale; they are
  reported, never capped, and never counted as code.

tokens/line is the anti-golf metric: a code-line count held down by stripping names and
packing statements shows up here. Stdlib ast + tokenize only.

Counting rules: a triple-quoted string literal is one token starting at one line, so an
embedded prose document (app.py's MANUAL) counts as ~1 code line by design — embedded
docs are effectively extra, and extracting one into a file will barely move code-lines.
That is intentional. It also means tokens/line reads low on string-heavy files
(manifest.py ~3.9 vs store.py ~7.0): compare like with like, and a falling tokens/line
on a file that merely gained string literals is not an improvement.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
from pathlib import Path

CORE_FILES = ("src/store.py", "src/didkey.py", "src/app.py")
EXTRA_FILES = ("src/manifest.py",)
BASELINE = Path(__file__).resolve().parent / "sz-baseline.json"
# Token types that carry no code: layout, comments, and the encoding/end markers.
_SKIP_TOKENS = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}


def _docstring_spans(tree):
    """Line numbers covered by module/class/function docstrings."""
    spans = set()

    def visit(node):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                for line in range(body[0].lineno, body[0].end_lineno + 1):
                    spans.add(line)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return spans


def measure(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_spans(tree)

    code_lines = set()
    comment_lines = set()
    tokens = 0
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in _SKIP_TOKENS or tok.start[0] in docstrings:
            continue
        code_lines.add(tok.start[0])
        tokens += 1

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])

    raw = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
    code = len(code_lines)
    return {
        "raw_lines": raw,
        "code_lines": code,
        "comment_lines": len(comment_lines),
        "tokens_per_line": round(tokens / code, 2) if code else 0.0,
    }


def measure_all(root):
    labeled = [("core/" + Path(f).name, f) for f in CORE_FILES]
    labeled += [("extra/" + Path(f).name, f) for f in EXTRA_FILES]
    return {label: measure(root / rel) for label, rel in labeled}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare against sz-baseline.json")
    parser.add_argument(
        "--update-baseline", action="store_true", help="rewrite sz-baseline.json from the tree"
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    files = measure_all(root)
    core_total = sum(v["code_lines"] for k, v in files.items() if k.startswith("core/"))

    if args.update_baseline:
        BASELINE.write_text(
            json.dumps({"core_total": core_total, "files": files}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"baseline written: core code-lines = {core_total}")

    header = f"{'file':<24} {'raw':>6} {'code':>6} {'comments':>9} {'tok/line':>9}"
    print(header)
    for label, m in files.items():
        print(
            f"{label:<24} {m['raw_lines']:>6} {m['code_lines']:>6}"
            f" {m['comment_lines']:>9} {m['tokens_per_line']:>9}"
        )
    print(f"{'core total (code)':<24} {'':>6} {core_total:>6}")

    if args.check:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        grown = [
            label
            for label, m in files.items()
            if label.startswith("core/")
            and m["code_lines"] > baseline["files"][label]["code_lines"]
        ]
        if grown:
            detail = ", ".join(
                f"{g} {baseline['files'][g]['code_lines']} -> {files[g]['code_lines']}"
                for g in grown
            )
            print(f"core code-lines grew vs sz-baseline.json: {detail}", file=sys.stderr)
            return 1
        if core_total > baseline["core_total"]:
            print(
                f"core total grew vs baseline: {baseline['core_total']} -> {core_total}",
                file=sys.stderr,
            )
            return 1
        print(f"check ok: core code-lines <= baseline ({baseline['core_total']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
