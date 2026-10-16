"""Resolve import bindings and type-bearing children of consumer annotations."""
from __future__ import annotations

import ast

from quard.marker_utils import import_base

TUPLES = frozenset({"builtins.tuple", "typing.Tuple"})
STAR = "*"
TUPLE = "tuple"


def imports(path: str, item: ast.Import | ast.ImportFrom) -> dict[str, str]:
    """Map imported aliases to their declared origin.

    Args:
        path (str): Source or stub path.
        item (ast.Import | ast.ImportFrom): Import declaration.

    Returns:
        dict[str, str]: Bound names and origins.
    """
    if isinstance(item, ast.Import):
        return {alias.asname or alias.name.split(".")[0]:
                alias.name if alias.asname else alias.name.split(".")[0] for alias in item.names}
    return {alias.asname or alias.name: f"{import_base(path, item)}.{alias.name}"
            for alias in item.names if alias.name != STAR}


def bindings(path: str, tree: ast.Module) -> dict[str, str | ast.expr | None]:
    """Collect top-level declarations and last known bindings in one file.

    Args:
        path (str): Source or stub path.
        tree (ast.Module): Parsed declarations.

    Returns:
        dict[str, str | ast.expr | None]: Names and evidence.
    """
    entries: dict[str, str | ast.expr | None] = {}
    for item in tree.body:
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            entries.update(imports(path, item))
        elif isinstance(item, ast.TypeAlias):
            entries[item.name.id] = item.value
        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            entries.update({target.id: item.value for target in targets
                            if isinstance(target, ast.Name)})
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            entries[item.name] = None
    return entries


def children(node: ast.expr, reference: str | None) -> list[ast.expr]:
    """Walk type positions, excluding Literal values and Annotated metadata.

    Args:
        node (ast.expr): Annotation expression.
        reference (str | None): Resolved generic origin.

    Returns:
        list[ast.expr]: Type-bearing children.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        parsed: list[ast.expr] = []
        try:
            parsed.append(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            pass
        return parsed
    if isinstance(node, ast.Subscript) and reference in {
            "typing.Literal", "typing_extensions.Literal"}:
        return []
    if isinstance(node, ast.Subscript) and reference in {
            "typing.Annotated", "typing_extensions.Annotated"}:
        return [node.slice.elts[0] if isinstance(node.slice, ast.Tuple) else node.slice]
    return [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)]
