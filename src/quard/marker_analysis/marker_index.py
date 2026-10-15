"""Binding-aware recognition of consumer-supplied identity markers."""
from __future__ import annotations

import ast
from typing import override

from quard.marker_analysis.marker_analysis import MarkerAnalysis
from quard.marker_utils import (FUNCTIONS, MARKER, PROPERTY_DECORATORS,
                                identity_definition, import_base, local_names, module_name)

__all__ = ["FUNCTIONS", "MARKER", "MarkerIndex", "identity_definition", "import_base",
           "local_names", "module_name"]


class MarkerIndex(MarkerAnalysis):
    """Keep per-declaration decorators, including separate source/stub evidence."""

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__()
        from quard.marker_binding_analysis.marker_bindings import MarkerBindings

        self.modules = {module_name(path) for path in trees}
        self.exports: dict[str, str | None] = {}
        self.decorators: dict[ast.expr, str | None] = {}
        self.descriptor_assignments: dict[ast.stmt, str] = {}
        for _ in range(len(trees) + 2):
            previous = self.exports.copy()
            self.decorators.clear()
            self.descriptor_assignments.clear()
            for path, tree in trees.items():
                bindings = MarkerBindings(self, path, self.exports, frozenset(self.modules))
                bindings.suite(tree.body)
                for name, value in bindings.module_bindings().items():
                    if "." not in name:
                        self.exports[f"{module_name(path)}.{name}"] = value
            if self.exports == previous:
                break

    @override
    def is_identity(self, node: ast.expr) -> bool:
        return self.decorators.get(node) == MARKER

    @override
    def symbol(self, node: ast.expr) -> str | None:
        return self.decorators.get(node)

    @override
    def record_decorator(self, node: ast.expr, symbol: str | None) -> None:
        self.decorators[node] = symbol

    @override
    def record_descriptor(self, node: ast.stmt, kind: str) -> None:
        self.descriptor_assignments[node] = kind

    @override
    def descriptor_kind(self, node: ast.stmt) -> str | None:
        return self.descriptor_assignments.get(node)

    @override
    def property_declarations(self, node: ast.ClassDef) -> dict[str, list[tuple[ast.stmt, str, str]]]:
        """Return proven descriptor declarations without collapsing accessors."""
        result: dict[str, list[tuple[ast.stmt, str, str]]] = {}
        for item in node.body:
            if isinstance(item, FUNCTIONS):
                for dec in item.decorator_list:
                    symbol = self.symbol(dec)
                    if symbol in PROPERTY_DECORATORS:
                        result.setdefault(item.name, []).append((item, "getter", symbol))
                    elif (symbol in {"builtins.property.setter", "builtins.property.deleter",
                                      "builtins.property.getter"} and isinstance(dec, ast.Attribute)
                          and isinstance(dec.value, ast.Name)):
                        result.setdefault(dec.value.id, []).append((item, dec.attr, "builtins.property"))
            elif isinstance(item, (ast.Assign, ast.AnnAssign)) and item in self.descriptor_assignments:
                kind = self.descriptor_assignments[item]
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        result.setdefault(target.id, []).append((item, "constructor", kind))
        return result
