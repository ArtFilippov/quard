"""Collect immediate class-suite stores from the class visitor."""
from __future__ import annotations

import ast

def class_stores(node: ast.ClassDef) -> list[tuple[str, ast.expr, bool, ast.expr | None]]:
    """Find stores in the class suite without visiting nested class/function bodies."""
    from eryx.field_ownership.class_stores import ClassStores

    visitor = ClassStores()
    for statement in node.body:
        visitor.visit(statement)
    return visitor.stores
