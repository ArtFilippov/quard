"""Resolve consumer-owned reflective attribute writes for the naming job."""

from __future__ import annotations

import ast
from typing import override

from quard.field_access.accesses import Accesses
from quard.field_ownership.bindings import Binding
from quard.field_ownership.ownership import Ownership
from quard.leading_underscores_scanner import BindingScanner


class AttributeScanner(Accesses):
    """Find writes only when the field index proves a first-party owner."""

    def __init__(self, ownership: Ownership, path: str, lines: list[str]) -> None:
        """Keep the owner's bindings and the source needed for positions.

        Args:
            ownership (Ownership): Index of discovered consumer classes.
            path (str): Project-relative source file.
            lines (list[str]): Source lines for locating attribute names.
        """
        super().__init__(ownership, path)
        self.names = BindingScanner(path, lines)

    @override
    def value(self, node: ast.expr) -> Binding | None:
        owner = super().dictionary_owner(node)
        if owner and owner[0] == "instance":
            return ("instance-dict", owner[1])
        return super().value(node)

    @override
    def dictionary_owner(self, node: ast.expr) -> Binding | None:
        owner = super().dictionary_owner(node)
        if owner:
            return owner
        binding = self.value(node) if isinstance(node, ast.Name) else None
        return ("instance", binding[1]) if binding and binding[0] == "instance-dict" else None

    @override
    def report_field(self, owner: Binding | None, field: str, ctx: ast.expr_context,
                     node: ast.expr, dictionary: bool = False) -> None:
        if (isinstance(ctx, ast.Store) and owner is not None
                and owner[0] in ({"instance"} if dictionary else {"instance", "class"})
                and not isinstance(node, ast.Call)):
            if dictionary and isinstance(node, ast.Subscript):
                node = node.slice
            self.names.add(field, node, locate=isinstance(node, ast.Attribute))

    @override
    def visit_Call(self, node: ast.Call) -> None:
        if (len(node.args) == 3 and not node.keywords and self.builtin_call(node, "setattr")
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            self.report_field(self.value(node.args[0]), node.args[1].value, ast.Store(),
                              node.args[1])
        if isinstance(node.func, ast.Attribute):
            owner = self.dictionary_owner(node.func.value)
            if owner and owner[0] == "instance":
                method = node.func.attr
                if ((method == "__setitem__" and len(node.args) == 2
                     or method == "setdefault" and len(node.args) in {1, 2})
                        and not node.keywords and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    self.report_field(owner, node.args[0].value, ast.Store(), node.args[0], True)
                if (method == "update" and len(node.args) == 1 and not node.keywords
                        and isinstance(node.args[0], ast.Dict)):
                    for key in node.args[0].keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            self.report_field(owner, key.value, ast.Store(), key, True)
                if method == "update" and not node.args:
                    for keyword in node.keywords:
                        if keyword.arg is not None:
                            self.names.add(keyword.arg, keyword, locate=True)
        self.generic_visit(node)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Accesses resolves methods and nested classes; also inspect executable
        # class-body writes using the surrounding module's proven bindings.
        super().visit_ClassDef(node)
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(statement)
