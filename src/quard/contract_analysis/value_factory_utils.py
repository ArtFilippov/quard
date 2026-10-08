"""Prove module-level NamedTuple value factories without importing consumer code."""
from __future__ import annotations

import ast
from typing import NamedTuple, cast

from quard.contract_analysis.contract_utils import dotted

FACTORIES = frozenset({"typing.NamedTuple", "typing_extensions.NamedTuple",
                       "collections.namedtuple"})
COLLECTIONS_FACTORY = "collections.namedtuple"
FIELD_PAIR = 2


def _conditional_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return names
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    for child in ast.iter_child_nodes(node):
        names.update(_conditional_names(child))
    return names


ValueFactory = NamedTuple("ValueFactory", [("assignment", ast.Assign),
                                         ("declaration", ast.ClassDef | None),
                                         ("identity", str | None)])


def _bindings(tree: ast.Module) -> dict[str, set[str | None]]:
    bindings: dict[str, set[str | None]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                name = alias.asname or alias.name.split(".")[0]
                bindings.setdefault(name, set()).add(alias.name if alias.asname else name)
        elif isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                name = alias.asname or alias.name
                if statement.level:
                    bindings.setdefault(name, set()).add(None)
                else:
                    bindings.setdefault(name, set()).add(f"{statement.module}.{alias.name}")
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, set()).add(None)
        elif isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.setdefault(statement.name, set()).add(None)
        else:
            for name in _conditional_names(statement):
                bindings.setdefault(name, set()).add(None)
    return bindings


def _fields(argument: ast.expr, identity: str) -> list[ast.stmt] | None:
    if identity == COLLECTIONS_FACTORY:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            names = argument.value.replace(",", " ").split()
        elif isinstance(argument, (ast.List, ast.Tuple)) and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in argument.elts):
            names = [cast(str, item.value) for item in argument.elts
                     if isinstance(item, ast.Constant)]
        else:
            return None
        if not all(name.isidentifier() for name in names):
            return None
        return [ast.copy_location(ast.AnnAssign(
            target=ast.copy_location(ast.Name(id=name, ctx=ast.Store()), argument),
            annotation=ast.copy_location(ast.Name(id="object", ctx=ast.Load()), argument),
            value=None, simple=1), argument) for name in names]
    if not isinstance(argument, (ast.List, ast.Tuple)):
        return None
    fields: list[ast.stmt] = []
    for field in argument.elts:
        if not isinstance(field, (ast.Tuple, ast.List)) or len(field.elts) != FIELD_PAIR:
            return None
        key = field.elts[0]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        if not key.value.isidentifier():
            return None
        fields.append(ast.copy_location(ast.AnnAssign(
            target=ast.copy_location(ast.Name(id=key.value, ctx=ast.Store()), field),
            annotation=field.elts[1], value=None, simple=1), field))
    return fields


def _evidence(statement: ast.Assign, bindings: dict[str, set[str | None]]
              ) -> ValueFactory | None:
    if not (len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Call)):
        return None
    call = statement.value
    if (text := dotted(call.func)) is None:
        return None
    first, _, rest = text.partition(".")
    origins = bindings.get(first, set())
    resolved = {origin + ("." + rest if rest else "") for origin in origins if origin}
    if not (resolved & FACTORIES or text in FACTORIES or
            text.split(".")[-1] in {"NamedTuple", "namedtuple"}):
        return None
    unknown = ValueFactory(statement, None, None)
    identity = next(iter(resolved)) if len(resolved) == 1 and None not in origins else None
    if identity not in FACTORIES or len(call.args) != FIELD_PAIR or call.keywords:
        return unknown
    if not isinstance(call.args[0], ast.Constant):
        return unknown
    if call.args[0].value != statement.targets[0].id:
        return unknown
    if (fields := _fields(call.args[1], identity)) is None:
        return unknown
    node = ast.copy_location(ast.ClassDef(name=statement.targets[0].id, bases=[],
                                          keywords=[], body=fields or [ast.Pass()],
                                          decorator_list=[], type_params=[]), statement)
    return ValueFactory(statement, node, identity)


def values(tree: ast.Module) -> list[ValueFactory]:
    """Find proven and uncertain factory-created values.

    Args:
        tree (ast.Module): Source or stub syntax tree.

    Returns:
        list[ValueFactory]: Located concrete or unresolved factory evidence.
    """
    bindings = _bindings(tree)
    return [evidence for statement in tree.body if isinstance(statement, ast.Assign)
            if (evidence := _evidence(statement, bindings)) is not None]
