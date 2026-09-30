"""Check consumer-owned class methods for instance binding."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

METHODS = ("staticmethod", "classmethod")
BUILTINS = (*METHODS, "setattr")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
IMPLICIT_CLASS_HOOKS = {"__new__", "__init_subclass__", "__class_getitem__"}


def needs_receiver(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check plain methods and abstract declarations; other decorators may change binding."""
    if method.name in IMPLICIT_CLASS_HOOKS:
        return False
    if method.decorator_list and not all(
            isinstance(decorator, ast.Name) and decorator.id == "abstractmethod"
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


class Declarations:
    """Track proven builtin bindings as class/module/function suites execute."""

    def __init__(self, path: str):
        self.path = path
        self.scopes: list[dict[str, str | None]] = [{}]
        self.classes: list[str] = []
        self.class_scopes = [False]
        self.class_ids: list[str] = []
        self.class_names: dict[str, str] = {}
        self.class_members: dict[str, dict[str, str | None]] = {}
        self.diagnostics: list[dict[str, object]] = []

    def binding(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return name if name in BUILTINS else None

    def decorator(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            value = self.binding(node.id)
            return value if value in METHODS else None
        if isinstance(node, ast.Attribute) and node.attr in METHODS:
            if self.value(node.value) == "builtins":
                return node.attr
        return None

    def value(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.binding(node.id)
        if isinstance(node, ast.Lambda):
            return "function"
        if isinstance(node, ast.Attribute):
            owner = self.value(node.value)
            if owner and owner.startswith("class:"):
                return self.class_members[owner].get(node.attr)
            if owner == "builtins" and node.attr in BUILTINS:
                return node.attr
        return self.decorator(node)

    def conversion(self, node: ast.expr) -> str | None:
        if (isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords
                and self.value(node.args[0]) == "function"):
            return self.decorator(node.func)
        return None

    def owner(self, target: ast.expr) -> tuple[str, str] | None:
        if isinstance(target, ast.Name) and self.class_scopes[-1]:
            return self.class_ids[-1], target.id
        if isinstance(target, ast.Attribute):
            class_id = self.value(target.value)
            if class_id and class_id.startswith("class:"):
                return class_id, target.attr
        return None

    def assign_target(self, target: ast.expr, value: str | None,
                      conversion: str | None, call: ast.expr | ast.stmt,
                      conditional: bool) -> None:
        owner = self.owner(target)
        if owner:
            class_id, name = owner
            if conversion:
                symbol = f"{self.class_names[class_id]}.{name}"
                self.diagnostics.append({
                    "rule": "NON_INSTANCE_METHOD", "path": self.path,
                    "line": call.lineno, "column": call.col_offset, "symbol": symbol,
                    "message": f"{symbol}: {conversion}(...) creates a non-instance method."})
            self.class_members[class_id][name] = None if conditional or conversion else value
        self.scopes[-1].update(dict.fromkeys(assigned(target), None if conditional else value))

    def setattr_call(self, node: ast.expr, conditional: bool) -> None:
        if (not isinstance(node, ast.Call) or len(node.args) != 3 or node.keywords
                or self.value(node.func) != "setattr" or not isinstance(node.args[1], ast.Constant)
                or not isinstance(node.args[1].value, str)):
            return
        owner = self.value(node.args[0])
        if owner and owner.startswith("class:"):
            target = self.class_members[owner]
            name = node.args[1].value
            conversion = self.conversion(node.args[2])
            if conversion:
                symbol = f"{self.class_names[owner]}.{name}"
                call = node.args[2]
                self.diagnostics.append({
                    "rule": "NON_INSTANCE_METHOD", "path": self.path,
                    "line": call.lineno, "column": call.col_offset, "symbol": symbol,
                    "message": f"{symbol}: {conversion}(...) creates a non-instance method."})
            target[name] = None if conditional or conversion else self.value(node.args[2])

    def suite(self, statements: list[ast.stmt], conditional: bool = False) -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name.split(".")[0]] = (
                        "builtins" if not conditional and alias.name == "builtins" else None)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name] = (
                         alias.name if not conditional and node.module == "builtins" and alias.name in BUILTINS else None)
            elif isinstance(node, ast.ClassDef):
                class_id = f"class:{id(node)}"
                self.class_names[class_id] = ".".join([*self.classes, node.name])
                self.class_members[class_id] = {}
                self.classes.append(node.name)
                self.scopes.append({})
                self.class_scopes.append(True)
                self.class_ids.append(class_id)
                self.suite(node.body)
                self.class_ids.pop()
                self.class_scopes.pop()
                self.scopes.pop()
                self.classes.pop()
                self.assign_target(ast.Name(id=node.name, ctx=ast.Store()),
                                   None if conditional or node.decorator_list else class_id,
                                   None, node, conditional)
            elif isinstance(node, FUNCTIONS):
                if self.class_scopes[-1]:
                    if needs_receiver(node):
                        symbol = ".".join([*self.classes, node.name])
                        self.diagnostics.append({
                            "rule": "MISSING_INSTANCE_RECEIVER", "path": self.path,
                            "line": node.lineno, "column": node.col_offset,
                            "symbol": symbol,
                            "message": f"{symbol}: instance method requires a mandatory positional receiver."})
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
                self.assign_target(ast.Name(id=node.name, ctx=ast.Store()),
                                   "function" if not node.decorator_list else None,
                                   None, node, conditional)
            elif isinstance(node, ast.Assign):
                self.setattr_call(node.value, conditional)
                value = None if conditional else self.value(node.value)
                for target in node.targets:
                    self.assign_target(target, value, self.conversion(node.value), node.value, conditional)
            elif isinstance(node, ast.AnnAssign):
                if node.value:
                    self.setattr_call(node.value, conditional)
                value = self.value(node.value) if node.value is not None and not conditional else None
                self.assign_target(node.target, value,
                                   self.conversion(node.value) if node.value else None,
                                   node.value or node, conditional)
            elif isinstance(node, ast.AugAssign):
                self.assign_target(node.target, None, None, node, conditional)
            elif isinstance(node, ast.Expr):
                self.setattr_call(node.value, conditional)
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
