"""Syntactic names and decorators used by cohesion ancestry resolution."""
from __future__ import annotations

import ast


METHOD = (ast.FunctionDef, ast.AsyncFunctionDef)
ABSTRACT_DECORATORS = frozenset({"abc.abstractmethod", "abc.abstractclassmethod",
                                 "abc.abstractstaticmethod", "abc.abstractproperty"})


def dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None
