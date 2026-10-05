"""Discover constructor bodies and run syntax checks with source positions."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import cast

from quard.constructor_origins.origins import Origins

RULE = "CONSTRUCTOR_PURITY"
PARSE_ERROR = "CONSTRUCTOR_PURITY_PARSE_ERROR"
EXECUTION_ERROR = "CONSTRUCTOR_PURITY_EXECUTION_ERROR"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def bindings(statements: list[ast.stmt]) -> set[str]:
    """Names local to a suite's scope, including conditional and later bindings."""
    names: set[str] = set()

    def collect(node: ast.AST) -> None:
        if isinstance(node, FUNCTIONS + (ast.ClassDef,)):
            names.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        if isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            collect(child)

    for statement in statements:
        collect(statement)
    return names


def analyze(root: Path, paths: list[object]) -> tuple[list[dict[str, object]], str | None]:
    from quard.constructor_analysis.constructor_checker import ConstructorChecker

    diagnostics: list[dict[str, object]] = []
    error = None
    trees: dict[str, ast.Module] = {}
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix != ".py"):
            raise ValueError(f"Invalid constructor purity source path: {path!r}")
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = PARSE_ERROR
            diagnostics.append({"rule": error, "path": path,
                                "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
            continue
        trees[path] = tree
    origins = Origins(trees)
    for path, tree in trees.items():
        checker = ConstructorChecker(path, origins)

        def walk(node: ast.AST, owners: tuple[str, ...] = ()) -> None:
            if isinstance(node, ast.ClassDef):
                qualified = ".".join((*owners, node.name))
                for child in node.body:
                    if isinstance(child, FUNCTIONS) and child.name == "__init__":
                        checker.constructor(child, qualified, node)
                    walk(child, (*owners, node.name))
            elif isinstance(node, FUNCTIONS):
                for child in node.body:
                    walk(child, owners)
            else:
                for child in ast.iter_child_nodes(node):
                    walk(child, owners)

        walk(tree)
        diagnostics.extend(checker.diagnostics)
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check eager syntax in discovered Python constructor bodies without executing source.")
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Constructor purity manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), cast(list[object], paths))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = EXECUTION_ERROR
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"quard_constructor_purity_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
