"""Shared source and stub names and class declarations for contract analysis."""
from __future__ import annotations

import ast
from pathlib import Path


def dotted(node: ast.expr) -> str | None:
    """Return a syntactic dotted name, unwrapping generic base arguments."""
    if isinstance(node, ast.Subscript):
        return dotted(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def module_name(path: str) -> str:
    """Map a discovered source path to its importable module name."""
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def collect_nodes(module: str, tree: ast.Module) -> dict[str, ast.ClassDef]:
    nodes: dict[str, ast.ClassDef] = {}

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        if isinstance(node, ast.ClassDef):
            parents = (*parents, node.name)
            nodes[".".join((module, *parents))] = node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parents = (*parents, node.name)
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree, ())
    return nodes
