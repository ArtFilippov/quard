"""Resolve local class ancestry for cohesion scoring."""
from __future__ import annotations

import ast
from typing import override

from quard.cohesion_analysis.cohesion_analysis import CohesionAnalysis
from quard.cohesion_analysis.declaration_utils import ABSTRACT_DECORATORS, METHOD, dotted

__all__ = ["ABSTRACT_DECORATORS", "METHOD", "dotted", "Declarations"]


class Declarations(CohesionAnalysis):
    """Resolve only local class inheritance and explicit stdlib typing/abc imports."""

    def __init__(self, tree: ast.Module):
        super().__init__()
        self.imports: dict[str, str] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        for item in tree.body:
            if isinstance(item, ast.Import):
                for alias in item.names:
                    if alias.name in {"abc", "typing", "typing_extensions"}:
                        self.imports[alias.asname or alias.name] = alias.name
            elif isinstance(item, ast.ImportFrom) and item.module in {"abc", "typing", "typing_extensions"}:
                for alias in item.names:
                    self.imports[alias.asname or alias.name] = f"{item.module}.{alias.name}"
        self._collect(tree.body, "")

    def _collect(self, body: list[ast.stmt], prefix: str) -> None:
        for item in body:
            if isinstance(item, ast.ClassDef):
                name = f"{prefix}.{item.name}" if prefix else item.name
                self.classes[name] = item
                self._collect(item.body, name)
            else:
                for child in ast.iter_child_nodes(item):
                    if isinstance(child, ast.stmt):
                        self._collect([child], prefix)

    @override
    def imported(self, expr: ast.expr) -> str | None:
        name = dotted(expr)
        if name is None:
            return None
        first, _, rest = name.partition(".")
        if first in self.classes:
            return None
        target = self.imports.get(first)
        return f"{target}.{rest}" if target and rest else target

    @override
    def base(self, expr: ast.expr, scope: str) -> str | None:
        name = dotted(expr)
        if name is None:
            return None
        prefixes = scope.split(".")
        for size in range(len(prefixes), -1, -1):
            candidate = ".".join([*prefixes[:size], name]) if size else name
            if candidate in self.classes:
                return candidate
        return None

    @override
    def decorated(self, method: ast.FunctionDef | ast.AsyncFunctionDef, name: str, module: str) -> bool:
        return any(self.imported(decorator) == f"{module}.{name}" for decorator in method.decorator_list)

    @override
    def is_abstract(self, method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(self.imported(decorator) in ABSTRACT_DECORATORS for decorator in method.decorator_list)

    @override
    def protocol(self, name: str) -> bool:
        node = self.classes[name]
        return any(self.imported(base) in {"typing.Protocol", "typing_extensions.Protocol"}
                   for base in node.bases)

    @override
    def abstract_methods(self, name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if name in seen:
            return set()
        node = self.classes[name]
        scope = name.rpartition(".")[0]
        outstanding: set[str] = set()
        for base in node.bases:
            parent = self.base(base, scope)
            if parent:
                outstanding.update(self.abstract_methods(parent, seen | {name}))
        for method in (item for item in node.body if isinstance(item, METHOD)):
            if self.is_abstract(method):
                outstanding.add(method.name)
            else:
                outstanding.discard(method.name)
        return outstanding

    @override
    def abc(self, name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in seen:
            return False
        node = self.classes[name]
        if any(self.imported(keyword.value) == "abc.ABCMeta"
               for keyword in node.keywords if keyword.arg == "metaclass"):
            return True
        scope = name.rpartition(".")[0]
        for base in node.bases:
            if self.imported(base) == "abc.ABC":
                return True
            parent = self.base(base, scope)
            if parent and self.abc(parent, seen | {name}):
                return True
        return False
