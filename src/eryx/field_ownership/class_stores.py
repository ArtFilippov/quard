"""Find class-namespace stores without entering deferred bodies."""
from __future__ import annotations

import ast


class ClassStores(ast.NodeVisitor):
    """Visit immediate class-suite stores, including compound statements."""

    def __init__(self) -> None:
        self.stores: list[tuple[str, ast.expr, bool, ast.expr | None]] = []

    def targets(self, target: ast.expr, value: ast.expr | None = None,
                annotated: bool = False) -> None:
        if isinstance(target, ast.Name):
            self.stores.append((target.id, target, annotated, value))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.targets(item, value, annotated)

    def visit_Assign(self, statement: ast.Assign) -> None:
        for target in statement.targets:
            self.targets(target, statement.value)
        self.visit(statement.value)

    def visit_AnnAssign(self, statement: ast.AnnAssign) -> None:
        if statement.value is not None:
            self.targets(statement.target, statement.value, annotated=True)
            self.visit(statement.value)

    def visit_AugAssign(self, statement: ast.AugAssign) -> None:
        self.targets(statement.target)
        self.visit(statement.value)

    def visit_NamedExpr(self, statement: ast.NamedExpr) -> None:
        self.targets(statement.target, statement.value)
        self.visit(statement.value)

    def visit_For(self, statement: ast.For | ast.AsyncFor) -> None:
        self.targets(statement.target)
        self.generic_visit(statement)

    def visit_AsyncFor(self, statement: ast.AsyncFor) -> None:
        self.visit_For(statement)

    def visit_With(self, statement: ast.With | ast.AsyncWith) -> None:
        for item in statement.items:
            if item.optional_vars:
                self.targets(item.optional_vars)
        self.generic_visit(statement)

    def visit_AsyncWith(self, statement: ast.AsyncWith) -> None:
        self.visit_With(statement)

    def visit_FunctionDef(self, unused: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        pass

    def visit_AsyncFunctionDef(self, unused: ast.AsyncFunctionDef) -> None:
        pass

    def visit_ClassDef(self, unused: ast.ClassDef) -> None:
        pass

    def visit_ListComp(self, unused: ast.ListComp) -> None:
        pass

    def visit_SetComp(self, unused: ast.SetComp) -> None:
        pass

    def visit_DictComp(self, unused: ast.DictComp) -> None:
        pass

    def visit_GeneratorExp(self, unused: ast.GeneratorExp) -> None:
        pass


from eryx.field_ownership.class_stores_utils import class_stores
