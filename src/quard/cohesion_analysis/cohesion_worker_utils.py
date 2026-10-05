"""Parse selected Python files and emit cohesion diagnostics."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from quard.cohesion_analysis.declarations import Declarations


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    from quard.cohesion_analysis.classes import Classes

    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if not isinstance(path, str) or not path.endswith(".py") or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"Invalid LCOM4 source path: {path!r}")
        try:
            source = (root / path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "LCOM4_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
        visitor = Classes(path, Declarations(tree))
        visitor.visit(tree)
        diagnostics.extend(visitor.results())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute basic class cohesion from the consumer's discovered Python files.")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("LCOM4 manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        diagnostics, error = ([{"rule": "LCOM4_EXECUTION_ERROR", "message": str(exc),
                                "is_infrastructure_error": True}], "LCOM4_EXECUTION_ERROR")
    print(json.dumps({"quard_lcom4_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
