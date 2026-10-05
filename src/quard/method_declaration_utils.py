"""Method binding and worker helpers, separate from the declaration visitor."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from quard.marker_analysis.marker_index import MarkerIndex

METHODS = ("staticmethod", "classmethod")
BUILTINS = (*METHODS, "setattr")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
IMPLICIT_CLASS_HOOKS = {"__new__", "__init_subclass__", "__class_getitem__"}


def needs_receiver(method: ast.FunctionDef | ast.AsyncFunctionDef,
                   markers: MarkerIndex | None = None) -> bool:
    """Check plain methods and abstract declarations; other decorators may change binding."""
    if method.name in IMPLICIT_CLASS_HOOKS:
        return False
    if method.decorator_list and not all(
             markers is not None and markers.is_identity(decorator)
             or isinstance(decorator, ast.Name) and decorator.id == "abstractmethod"
            or isinstance(decorator, ast.Attribute) and decorator.attr == "abstractmethod"
            for decorator in method.decorator_list):
        return False
    positional = method.args.posonlyargs + method.args.args
    return len(positional) <= len(method.args.defaults)


def assigned(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(assigned(item) for item in target.elts))
    if isinstance(target, ast.Starred):
        return assigned(target.value)
    return set()


def function_locals(method: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Function locals shadow builtins throughout the function, even before assignment."""
    names: set[str] = set()

    def collect(node: ast.AST) -> None:
        if isinstance(node, FUNCTIONS + (ast.ClassDef,)):
            names.add(node.name)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            collect(child)

    for statement in method.body:
        collect(statement)
    return names


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    from quard.method_analysis.declarations import Declarations

    diagnostics: list[dict[str, object]] = []
    trees: dict[str, ast.Module] = {}
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid method decorator source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "METHOD_DECORATORS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
    markers = MarkerIndex(trees)
    for path, tree in trees.items():
        visitor = Declarations(path, markers)
        visitor.suite(tree.body)
        diagnostics.extend(visitor.results())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description="Check consumer-owned class methods for instance binding.")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Method decorator manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        diagnostics, error = ([{"rule": "METHOD_DECORATORS_EXECUTION_ERROR", "message": str(exc),
                                "is_infrastructure_error": True}], "METHOD_DECORATORS_EXECUTION_ERROR")
    print(json.dumps({"quard_method_decorators_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)
