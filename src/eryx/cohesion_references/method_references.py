"""Collect cohesion edges from method bodies."""
from __future__ import annotations

import ast
from typing import override

from eryx.cohesion_references.cohesion_references import CohesionReferences


class MethodReferences(ast.NodeVisitor, CohesionReferences):
    """References in one method body, excluding deferred nested scopes."""

    def __init__(self, instance_receiver: str | None, class_receivers: set[str],
                 methods: set[str], declared: set[str]):
        self.instance_receiver = instance_receiver
        self.class_receivers = class_receivers
        self.methods = methods
        self.declared = declared
        self.calls: set[str] = set()
        self.fields: set[tuple[str, str]] = set()

    @override
    def receiver_kind(self, name: str) -> str | None:
        if name == self.instance_receiver:
            return "instance"
        if name in self.class_receivers:
            return "class"
        return None

    @override
    def connects_to(self, other: MethodReferences, method: str) -> bool:
        return method in self.calls or bool(self.fields & other.fields)

    @override
    def visit_Call(self, node: ast.Call) -> None:
        if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and self.receiver_kind(node.func.value.id) is not None):
            if node.func.attr in self.methods:
                self.calls.add(node.func.attr)
            # An undeclared call could be inherited or dynamically supplied; its
            # target is not evidence of a field shared with another method.
            for arg in node.args:
                self.visit(arg)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            self.generic_visit(node)

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.attr not in self.declared:
            kind = self.receiver_kind(node.value.id)
            if kind is not None:
                self.fields.add((kind, node.attr))
        self.generic_visit(node)

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        pass

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass
