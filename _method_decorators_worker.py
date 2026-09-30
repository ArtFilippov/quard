"""Reject declarations explicitly decorated with built-in non-instance descriptors."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

METHODS = ("staticmethod", "classmethod")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


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


class Declarations:
    """Track proven builtin bindings as class/module/function suites execute."""

    def __init__(self, path: str):
        self.path = path
        self.scopes: list[dict[str, str | None]] = [{}]
        self.classes: list[str] = []
        self.class_scopes = [False]
        self.diagnostics: list[dict[str, object]] = []

    def binding(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return name if name in METHODS else None

    def decorator(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            value = self.binding(node.id)
            return value if value in METHODS else None
        if isinstance(node, ast.Attribute) and node.attr in METHODS and isinstance(node.value, ast.Name):
            if self.binding(node.value.id) == "builtins":
                return node.attr
        return None

    def value(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.binding(node.id)
        return self.decorator(node)

    def suite(self, statements: list[ast.stmt], conditional: bool = False) -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name.split(".")[0]] = (
                        "builtins" if not conditional and alias.name == "builtins" else None)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name] = (
                        alias.name if not conditional and node.module == "builtins" and alias.name in METHODS else None)
            elif isinstance(node, ast.ClassDef):
                self.classes.append(node.name)
                self.scopes.append({})
                self.class_scopes.append(True)
                self.suite(node.body)
                self.class_scopes.pop()
                self.scopes.pop()
                self.classes.pop()
                self.scopes[-1][node.name] = None
            elif isinstance(node, FUNCTIONS):
                if self.class_scopes[-1]:
                    for decorator in node.decorator_list:
                        kind = self.decorator(decorator)
                        if kind:
                            symbol = ".".join([*self.classes, node.name])
                            self.diagnostics.append({
                                "rule": "NON_INSTANCE_METHOD", "path": self.path,
                                "line": decorator.lineno, "column": decorator.col_offset,
                                "symbol": symbol,
                                "message": f"{symbol}: @{kind} declares a non-instance method."})
                # A class inside a function may still be declared in consumer scope.
                # Python's function locals shadow globals even before assignment.
                self.scopes.append(dict.fromkeys(function_locals(node)))
                self.scopes[-1].update({arg.arg: None for arg in (
                    node.args.posonlyargs + node.args.args + node.args.kwonlyargs)})
                self.class_scopes.append(False)
                if node.args.vararg:
                    self.scopes[-1][node.args.vararg.arg] = None
                if node.args.kwarg:
                    self.scopes[-1][node.args.kwarg.arg] = None
                self.suite(node.body)
                self.class_scopes.pop()
                self.scopes.pop()
                self.scopes[-1][node.name] = None
            elif isinstance(node, ast.Assign):
                value = None if conditional else self.value(node.value)
                for target in node.targets:
                    self.scopes[-1].update(dict.fromkeys(assigned(target), value))
            elif isinstance(node, ast.AnnAssign):
                value = self.value(node.value) if node.value is not None and not conditional else None
                self.scopes[-1].update(dict.fromkeys(assigned(node.target), value))
            elif isinstance(node, ast.AugAssign):
                self.scopes[-1].update(dict.fromkeys(assigned(node.target)))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                self.scopes[-1].update(dict.fromkeys(assigned(node.target)))
                self.suite(node.body, True)
                self.suite(node.orelse, True)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        self.scopes[-1].update(dict.fromkeys(assigned(item.optional_vars)))
                self.suite(node.body, True)
            elif isinstance(node, ast.If):
                self.suite(node.body, True)
                self.suite(node.orelse, True)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                self.suite(node.body, True)
                for handler in node.handlers:
                    if handler.name:
                        self.scopes[-1][handler.name] = None
                    self.suite(handler.body, True)
                self.suite(node.orelse, True)
                self.suite(node.finalbody, True)
            elif isinstance(node, ast.While):
                self.suite(node.body, True)
                self.suite(node.orelse, True)
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    self.suite(case.body, True)


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid method decorator source path: {path!r}")
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "METHOD_DECORATORS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
        visitor = Declarations(path)
        visitor.suite(tree.body)
        diagnostics.extend(visitor.diagnostics)
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    print(json.dumps({"eryx_method_decorators_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
