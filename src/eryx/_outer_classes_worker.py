"""Check that each discovered Python source or stub has at most one outer class."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast


def outer_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Count classes unless an enclosing class contains the declaration."""
    classes: list[ast.ClassDef] = []

    def collect(node: ast.AST, enclosed: bool) -> None:
        if isinstance(node, ast.ClassDef):
            if not enclosed:
                classes.append(node)
            enclosed = True
        for child in ast.iter_child_nodes(node):
            collect(child, enclosed)

    collect(tree, False)
    return classes


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid outer-class source path: {path!r}")
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, ValueError, SyntaxError) as exc:
            error = "OUTER_CLASSES_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
        classes = outer_classes(tree)
        if len(classes) > 1:
            for node in classes:
                message = f"{node.name}: {path} has {len(classes)} outer classes (max one per file)."
                diagnostics.append({"rule": "MULTIPLE_OUTER_CLASSES", "path": path,
                                    "line": node.lineno, "column": node.col_offset + 1,
                                    "symbol": node.name, "message": message})
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths: object = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Outer-class manifest must be a list of Python paths")
        values = cast(list[object], paths)
        if not all(isinstance(path, str) for path in values):
            raise ValueError("Outer-class manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), [path for path in values if isinstance(path, str)])
    except (OSError, ValueError, TypeError) as exc:
        error = "OUTER_CLASSES_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"eryx_outer_classes_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
