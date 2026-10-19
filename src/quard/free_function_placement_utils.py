"""Check free functions and class-free utilities modules in discovered sources."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast

PARENT_SEGMENT = ".."
SOURCE_ROOT = "src"
MAIN_FOLDER = "main"


def declarations(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node)
            return
        if isinstance(node, (ast.ClassDef, ast.Lambda)):
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return functions


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error: str | None = None
    for path in paths:
        source = Path(path)
        if (source.is_absolute() or PARENT_SEGMENT in source.parts
                or source.suffix not in {".py", ".pyi"}):
            error = "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"
            diagnostics.append({"rule": error, "path": path,
                                "message": f"Invalid free-function placement source path: {path!r}",
                                "is_infrastructure_error": True})
            continue
        try:
            tree = ast.parse((root / source).read_text(encoding="utf-8"), filename=path)
        except (OSError, ValueError, SyntaxError) as exc:
            error = "FREE_FUNCTION_PLACEMENT_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path,
                                "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
        classes = ([node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
                   if source.stem.endswith("_utils") and len(source.stem) > len("_utils") else [])
        for node in classes:
            diagnostics.append({"rule": "UTILITIES_MODULE_CLASS", "path": path,
                                "line": node.lineno, "column": node.col_offset + 1,
                                "symbol": node.name,
                                "message": (f"{node.name}: utilities module {path} "
                                            "must be class-free.")})
        if (not (source.parts[0] == SOURCE_ROOT and MAIN_FOLDER in source.parts[1:-1])
                and (classes or not (source.stem.endswith("_utils")
                                     and len(source.stem) > len("_utils")))):
            for node in declarations(tree):
                diagnostics.append({"rule": "FREE_FUNCTION_PLACEMENT", "path": path,
                                    "line": node.lineno, "column": node.col_offset + 1,
                                    "symbol": node.name,
                                    "message": (f"{node.name}: design this code around "
                                                + "object-oriented classes rather than free functions. "
                                                + "If a free function is unavoidable, "
                                                + "declare it in a class-free <group_name>_utils.py "
                                                + "module (or companion stub).")})
    return diagnostics, error


def read_manifest(path: Path) -> list[str]:
    paths: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(paths, list):
        raise ValueError("Free-function placement manifest must be a list of Python paths")
    entries = cast(list[object], paths)
    if any(not isinstance(entry, str) for entry in entries):
        raise ValueError("Free-function placement manifest must be a list of Python paths")
    return cast(list[str], entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        diagnostics, error = analyze(Path.cwd(), read_manifest(args.manifest))
    except (OSError, ValueError, TypeError) as exc:
        error = "FREE_FUNCTION_PLACEMENT_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"quard_free_function_placement_adapter": 1,
                      "diagnostics": diagnostics, "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
