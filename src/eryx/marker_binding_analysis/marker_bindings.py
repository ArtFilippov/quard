"""Resolve marker bindings at declaration time, respecting lexical scope."""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from eryx.marker_analysis.marker_index import (FUNCTIONS, MARKER, identity_definition,
                                               import_base, local_names)
from eryx.marker_binding_analysis.marker_binding_analysis import MarkerBindingAnalysis

if TYPE_CHECKING:
    from eryx.marker_analysis.marker_index import MarkerIndex


class MarkerBindings(MarkerBindingAnalysis):
    """Resolve decorator expressions at declaration time, respecting lexical binding."""

    def __init__(self, index: MarkerIndex, path: str):
        super().__init__()
        self.index = index
        self.path = path
        self.scopes: list[dict[str, str | None]] = [{}]
        self.class_scope = False

    def value(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            for scope in reversed(self.scopes):
                if node.id in scope:
                    return scope[node.id]
            return f"builtins.{node.id}" if node.id in {"property", "staticmethod", "classmethod"} else None
        if isinstance(node, ast.Attribute):
            owner = self.value(node.value)
            if owner and owner != MARKER:
                qualified = f"{owner}.{node.attr}"
                return self.imported(qualified)
        return None

    def imported(self, qualified: str) -> str | None:
        for scope in reversed(self.scopes):
            if qualified in scope:
                return scope[qualified]
        return self.index.imported(qualified)

    def assign(self, target: ast.expr, value: str | None) -> None:
        if isinstance(target, ast.Name):
            self.scopes[-1][target.id] = value
        elif isinstance(target, ast.Attribute):
            owner = self.value(target.value)
            if owner and owner != MARKER:
                self.scopes[-1][f"{owner}.{target.attr}"] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.assign(item, None)
        elif isinstance(target, ast.Starred):
            self.assign(target.value, None)

    def suite(self, body: list[ast.stmt], conditional: bool = False) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name.split(".")[0]] = (
                        None if conditional else alias.name if alias.asname else alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name] = (None if conditional else
                        self.imported(f"{import_base(self.path, node)}.{alias.name}"))
            elif isinstance(node, FUNCTIONS + (ast.ClassDef,)):
                for decorator in node.decorator_list:
                    self.index.decorators[decorator] = self.value(decorator)
                descriptor = next((self.value(dec) for dec in node.decorator_list
                                   if self.value(dec) in {"builtins.property", "functools.cached_property",
                                                         "builtins.property.setter", "builtins.property.deleter",
                                                         "builtins.property.getter"}), None)
                previous = self.class_scope
                class_namespace = self.scopes.pop() if previous and isinstance(node, FUNCTIONS) else None
                self.scopes.append(dict.fromkeys(local_names(node)) if isinstance(node, FUNCTIONS) else {})
                self.class_scope = isinstance(node, ast.ClassDef)
                self.suite(node.body)
                _ = self.scopes.pop()
                if class_namespace is not None:
                    self.scopes.append(class_namespace)
                self.class_scope = previous
                self.scopes[-1][node.name] = (MARKER if not conditional and isinstance(node, FUNCTIONS)
                                              and identity_definition(node, self.path) else
                                              "builtins.property" if not conditional and descriptor and
                                              descriptor.startswith("builtins.property") else
                                              descriptor if not conditional else None)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                if isinstance(node.value, ast.Call) and not conditional:
                    kind = self.value(node.value.func)
                    if kind in {"builtins.property", "functools.cached_property"}:
                        self.index.descriptor_assignments[node] = kind
                value = self.value(node.value) if node.value is not None and not conditional else None
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    self.assign(target, self.index.descriptor_assignments.get(node, value))
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, ast.Delete) else [node.target]
                for target in targets:
                    self.assign(target, None)
            else:
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    self.assign(node.target, None)
                if isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars is not None:
                            self.assign(item.optional_vars, None)
                # Conditional bindings cannot prove a marker, including after a branch.
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        self.suite([child], True)
                    elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                        if isinstance(child, ast.ExceptHandler) and child.name:
                            self.scopes[-1][child.name] = None
                        if isinstance(child, ast.match_case):
                            for pattern in ast.walk(child.pattern):
                                if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
                                    self.scopes[-1][pattern.name] = None
                        self.suite(child.body, True)
