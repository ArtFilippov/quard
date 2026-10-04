"""Recognize literal receiver-field operations and conservative receiver aliases."""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eryx.field_operation_model.field_operation import FieldOperation

def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _builtins(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    functions: dict[str, str] = {}
    modules: set[str] = set()
    for item in tree.body:
        if isinstance(item, ast.Import):
            for alias in item.names:
                if alias.name == "builtins":
                    modules.add(alias.asname or "builtins")
        elif isinstance(item, ast.ImportFrom) and item.level == 0 and item.module == "builtins":
            for alias in item.names:
                if alias.name in {"setattr", "delattr", "vars"}:
                    functions[alias.asname or alias.name] = alias.name
        elif isinstance(item, (ast.Assign, ast.AnnAssign, ast.FunctionDef,
                               ast.AsyncFunctionDef, ast.ClassDef)):
            targets = (item.targets if isinstance(item, ast.Assign) else [item.target]
                       if isinstance(item, ast.AnnAssign) else [])
            bound = {entry.id for target in targets for entry in ast.walk(target)
                     if isinstance(entry, ast.Name) and isinstance(entry.ctx, ast.Store)}
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(item.name)
            for name in bound:
                functions.pop(name, None)
                modules.discard(name)
    return functions, modules


def field_operations(tree: ast.Module, method: ast.FunctionDef | ast.AsyncFunctionDef,
                     receiver: str) -> dict[ast.stmt, list[FieldOperation]]:
    """Map statements to operations; uncertain aliases never establish writes.

    Args:
        tree (ast.Module): Module supplying explicit builtins imports.
        method (ast.FunctionDef | ast.AsyncFunctionDef): Owning instance method.
        receiver (str): Name of its instance receiver.

    Returns:
        dict[ast.stmt, list[FieldOperation]]: Proven operations by statement.
    """
    from eryx.field_operation_model.field_operation import FieldOperation

    functions, modules = _builtins(tree)
    locals_ = {arg.arg for arg in (*method.args.posonlyargs, *method.args.args,
                                   *method.args.kwonlyargs)}
    if method.args.vararg:
        locals_.add(method.args.vararg.arg)
    if method.args.kwarg:
        locals_.add(method.args.kwarg.arg)
    for statement in method.body:
        locals_.update(item.id for item in ast.walk(statement)
                       if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)))
    for statement in method.body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "builtins":
            for alias in statement.names:
                bound = alias.asname or alias.name
                if bound not in locals_ and alias.name in {"setattr", "delattr", "vars"}:
                    functions[bound] = alias.name
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound not in locals_ and alias.name == "builtins":
                    modules.add(bound)
        else:
            break  # Imports after executable statements cannot prove earlier calls.
    operations: dict[ast.stmt, list[FieldOperation]] = {}

    def builtin(node: ast.expr, name: str) -> bool:
        if isinstance(node, ast.Name):
            return (node.id == name and name not in locals_ and name not in functions
                    or node.id not in locals_ and functions.get(node.id) == name)
        return (isinstance(node, ast.Attribute) and node.attr == name
                and isinstance(node.value, ast.Name) and node.value.id in modules
                and node.value.id not in locals_)

    def owns(node: ast.expr, aliases: set[str]) -> bool:
        return isinstance(node, ast.Name) and node.id in aliases

    def dictionary(node: ast.expr, aliases: set[str]) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "__dict__"
                and owns(node.value, aliases) or isinstance(node, ast.Call)
                and builtin(node.func, "vars") and len(node.args) == 1 and not node.keywords
                and owns(node.args[0], aliases))

    def target(node: ast.expr, aliases: set[str], kind: str,
               value: ast.expr | None = None) -> list[FieldOperation]:
        if isinstance(node, ast.Attribute) and owns(node.value, aliases):
            return [FieldOperation(node.attr, kind, node, value)]
        if isinstance(node, ast.Subscript) and dictionary(node.value, aliases):
            name = _literal(node.slice)
            return [FieldOperation(name, kind, node, value)] if name is not None else []
        if isinstance(node, (ast.Tuple, ast.List)):
            return [op for part in node.elts for op in target(part, aliases, kind, value)]
        return []

    def calls(node: ast.AST, aliases: set[str]) -> list[FieldOperation]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                             ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            return []
        found: list[FieldOperation] = []
        if isinstance(node, ast.Call):
            if (len(node.args) in {2, 3} and not node.keywords
                    and owns(node.args[0], aliases) and (name := _literal(node.args[1])) is not None):
                if builtin(node.func, "setattr") and len(node.args) == 3:
                    found.append(FieldOperation(name, "write", node, node.args[2]))
                elif builtin(node.func, "delattr") and len(node.args) == 2:
                    found.append(FieldOperation(name, "delete", node))
            if isinstance(node.func, ast.Attribute) and dictionary(node.func.value, aliases):
                method_name = node.func.attr
                if node.args and (name := _literal(node.args[0])) is not None:
                    kind = {"__setitem__": "write", "setdefault": "setdefault",
                            "pop": "delete", "__delitem__": "delete"}.get(method_name)
                    if kind and not node.keywords:
                        value = node.args[1] if kind in {"write", "setdefault"} and len(node.args) > 1 else None
                        found.append(FieldOperation(name, kind, node, value))
                if (method_name == "update" and len(node.args) == 1 and not node.keywords
                        and isinstance(node.args[0], ast.Dict)):
                    found.extend(FieldOperation(name, "write", node, value)
                                 for key, value in zip(node.args[0].keys, node.args[0].values)
                                 if key is not None and (name := _literal(key)) is not None)
        for child in ast.iter_child_nodes(node):
            found.extend(calls(child, aliases))
        return found

    def block(body: list[ast.stmt], aliases: set[str]) -> set[str]:
        current = aliases.copy()
        for statement in body:
            before = current.copy()
            found: list[FieldOperation] = []
            if isinstance(statement, ast.Assign):
                found.extend(calls(statement.value, before))
                for item in statement.targets:
                    found.extend(target(item, before, "write", statement.value))
                    if isinstance(item, ast.Name):
                        current.discard(item.id)
                        if owns(statement.value, before):
                            current.add(item.id)
            elif isinstance(statement, ast.AnnAssign):
                if statement.value:
                    found.extend(calls(statement.value, before))
                    found.extend(target(statement.target, before, "write", statement.value))
                if isinstance(statement.target, ast.Name):
                    current.discard(statement.target.id)
                    if statement.value and owns(statement.value, before):
                        current.add(statement.target.id)
            elif isinstance(statement, ast.AugAssign):
                found.extend(target(statement.target, before, "augment"))
                found.extend(calls(statement.value, before))
                if isinstance(statement.target, ast.Name):
                    current.discard(statement.target.id)
            elif isinstance(statement, ast.Delete):
                for item in statement.targets:
                    found.extend(target(item, before, "delete"))
                    if isinstance(item, ast.Name):
                        current.discard(item.id)
            elif isinstance(statement, ast.If):
                found.extend(calls(statement.test, before))
                current = block(statement.body, before) & block(statement.orelse, before)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                found.extend(calls(statement.iter if isinstance(statement, (ast.For, ast.AsyncFor))
                                   else statement.test, before))
                loop_aliases = before - {item.id for item in ast.walk(statement.target)
                                         if isinstance(item, ast.Name)
                                         and isinstance(item.ctx, ast.Store)} if isinstance(
                                             statement, (ast.For, ast.AsyncFor)) else before
                _ = block(statement.body, loop_aliases)
                _ = block(statement.orelse, before)
                current = before - {item.id for item in ast.walk(statement)
                                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    found.extend(calls(item.context_expr, before))
                    if item.optional_vars:
                        found.extend(target(item.optional_vars, before, "write"))
                        current -= {entry.id for entry in ast.walk(item.optional_vars)
                                    if isinstance(entry, ast.Name)
                                    and isinstance(entry.ctx, ast.Store)}
                current = block(statement.body, current)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                main = block(statement.orelse, block(statement.body, before))
                handled = [block(handler.body, before) for handler in statement.handlers]
                current = main
                for handler_aliases in handled:
                    current &= handler_aliases
                current = block(statement.finalbody, current)
            elif isinstance(statement, ast.Match):
                found.extend(calls(statement.subject, before))
                exhaustive = False
                joined: set[str] | None = None
                for case in statement.cases:
                    case_aliases = block(case.body, before - {
                        item.name for item in ast.walk(case.pattern)
                        if isinstance(item, ast.MatchAs) and item.name is not None})
                    joined = case_aliases if joined is None else joined & case_aliases
                    if (case.guard is None and isinstance(case.pattern, ast.MatchAs)
                            and case.pattern.pattern is None):
                        exhaustive = True
                current = joined if exhaustive and joined is not None else before & (joined or set())
            elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.extend(calls(statement, before))
            if found:
                operations[statement] = sorted(found, key=lambda op: (op.location.lineno,
                                                                       op.location.col_offset))
        return current

    _ = block(method.body, {receiver})
    return operations
