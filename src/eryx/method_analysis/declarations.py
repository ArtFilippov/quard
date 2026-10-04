"""Check consumer-owned class methods for instance binding."""
from __future__ import annotations

import ast
import sys
from typing import override

from eryx.marker_analysis.marker_index import MarkerIndex
from eryx.method_analysis.method_analysis import MethodAnalysis
from eryx.method_declaration_utils import (BUILTINS, FUNCTIONS, IMPLICIT_CLASS_HOOKS, METHODS,
                                           analyze, assigned, function_locals, main, needs_receiver)

__all__ = ["BUILTINS", "FUNCTIONS", "IMPLICIT_CLASS_HOOKS", "METHODS", "Declarations",
           "analyze", "assigned", "function_locals", "main", "needs_receiver"]


class Declarations(MethodAnalysis):
    """Track proven builtin bindings as class/module/function suites execute."""

    def __init__(self, path: str, markers: MarkerIndex | None = None):
        self.path = path
        self.markers = markers
        self.scopes: list[dict[str, str | None]] = [{}]
        self.classes: list[str] = []
        self.class_scopes = [False]
        self.class_ids: list[str] = []
        self.class_names: dict[str, str] = {}
        self.class_members: dict[str, dict[str, str | None]] = {}
        self.diagnostics: list[dict[str, object]] = []

    @override
    def binding(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return name if name in BUILTINS else None

    @override
    def results(self) -> list[dict[str, object]]:
        return self.diagnostics

    @override
    def decorator(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            value = self.binding(node.id)
            return value if value in METHODS else None
        if isinstance(node, ast.Attribute) and node.attr in METHODS:
            if self.value(node.value) == "builtins":
                return node.attr
        return None

    @override
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

    @override
    def conversion(self, node: ast.expr) -> str | None:
        if (isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords
                and self.value(node.args[0]) == "function"):
            return self.decorator(node.func)
        return None

    @override
    def owner(self, target: ast.expr) -> tuple[str, str] | None:
        if isinstance(target, ast.Name) and self.class_scopes[-1]:
            return self.class_ids[-1], target.id
        if isinstance(target, ast.Attribute):
            class_id = self.value(target.value)
            if class_id and class_id.startswith("class:"):
                return class_id, target.attr
        return None

    @override
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

    @override
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

    @override
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
                                    None if conditional or any(
                                        self.markers is None or not self.markers.is_identity(dec)
                                        for dec in node.decorator_list) else class_id,
                                   None, node, conditional)
            elif isinstance(node, FUNCTIONS):
                if self.class_scopes[-1]:
                    if needs_receiver(node, self.markers):
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
                                    "function" if all(self.markers is not None and
                                                      self.markers.is_identity(dec)
                                                      for dec in node.decorator_list) else None,
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


if __name__ == "__main__":
    sys.exit(main())
