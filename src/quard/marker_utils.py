"""Pure helpers for resolving consumer identity markers."""
from __future__ import annotations

import ast
from pathlib import Path

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
MARKER = "<encapsulated_only>"
PROPERTY_DECORATORS = frozenset({"builtins.property", "functools.cached_property",
                                 "qraft.cached_property"})


def module_name(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_base(path: str, node: ast.ImportFrom) -> str:
    base = node.module or ""
    if not node.level:
        return base
    module = module_name(path)
    package = module if Path(path).stem == "__init__" else module.rpartition(".")[0]
    parts = package.split(".") if package else []
    return ".".join([*parts[:len(parts) - node.level + 1], *([base] if base else [])])


def local_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Collect lexical locals without leaking names from deferred child scopes."""
    names = {arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    if node.args.vararg:
        names.add(node.args.vararg.arg)
    if node.args.kwarg:
        names.add(node.args.kwarg.arg)
    pending: list[ast.AST] = list(node.body)
    while pending:
        item = pending.pop()
        if isinstance(item, FUNCTIONS + (ast.ClassDef,)):
            names.add(item.name)
            continue
        if isinstance(item, ast.Lambda):
            continue
        if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
            names.add(item.id)
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in item.names)
        if isinstance(item, ast.ExceptHandler) and item.name:
            names.add(item.name)
        pending.extend(ast.iter_child_nodes(item))
    return names


def identity_definition(node: ast.FunctionDef | ast.AsyncFunctionDef, path: str) -> bool:
    """Source markers return their sole argument; stubs declare the same convention."""
    args = node.args.posonlyargs + node.args.args
    if (node.name != "encapsulated_only" or isinstance(node, ast.AsyncFunctionDef)
            or node.decorator_list or len(args) != 1 or node.args.vararg or node.args.kwarg
            or node.args.kwonlyargs):
        return False
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if len(body) != 1:
        return False
    statement = body[0]
    return (isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
            and statement.value.id == args[0].arg or path.endswith(".pyi")
            and isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis)
