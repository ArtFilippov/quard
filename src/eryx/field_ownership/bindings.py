"""Shared names and builtin bindings for field owners and access sites."""
from __future__ import annotations

import ast
from pathlib import Path

Binding = tuple[str, str]  # (class | instance | module | builtins | builtin, name)
PROTOCOL_BUILTINS = frozenset({"len", "str", "repr", "ascii", "format", "int", "float",
                               "complex", "bin", "oct", "hex", "abs", "round", "hash",
                               "iter", "next", "list", "tuple", "any", "all"})


def assigned_names(node: ast.AST) -> set[str]:
    return {item.id for item in ast.walk(node)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}


def module_name(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)
