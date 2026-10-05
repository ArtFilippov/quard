"""Collect source and stub evidence for stored-field types."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from quard.field_operation_model.operation_utils import field_operations

if TYPE_CHECKING:
    from quard.field_type_analysis.type_evidence import TypeEvidence

UNKNOWN = "?"


def _module(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def field_evidence(node: ast.ClassDef, fields: set[str], module: str,
                   types: TypeEvidence, tree: ast.Module | None = None
                   ) -> dict[str, tuple[list[str], list[str]]]:
    """Collect explicit annotations and unannotated write types per field."""
    evidence: dict[str, tuple[list[str], list[str]]] = {field: ([], []) for field in fields}
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if item.target.id in fields:
                evidence[item.target.id][0].append(types.annotation(item.annotation, module))
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = item.args.posonlyargs + item.args.args
        if not args:
            continue
        receiver = args[0].arg
        locals_ = {arg.arg for arg in (*args, *item.args.kwonlyargs)}
        parameters = {arg.arg: types.annotation(arg.annotation, module) for arg in
                       (*args, *item.args.kwonlyargs) if arg.annotation is not None}
        if tree is not None:
            for statement, events in field_operations(tree, item, receiver).items():
                for event in events:
                    if (event.name not in fields or event.kind not in {"write", "setdefault"}
                            or event.kind == "setdefault" and item.name != "__init__"):
                        continue
                    if isinstance(event.location, ast.Attribute) and isinstance(
                            event.location.value, ast.Name) and event.location.value.id == receiver:
                        continue  # The direct write below supplies annotation-aware evidence.
                    if isinstance(statement, ast.AnnAssign) and statement.target is event.location:
                        evidence[event.name][0].append(types.annotation(
                            statement.annotation, module, locals_))
                        continue
                    expression = event.value
                    inferred = (parameters.get(expression.id, UNKNOWN)
                                if isinstance(expression, ast.Name) else
                                types.expression(expression, module, locals_)
                                if expression is not None else UNKNOWN)
                    evidence[event.name][1].append(inferred)
        pending: list[ast.AST] = list(item.body)
        while pending:
            child = pending.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            pending.extend(ast.iter_child_nodes(child))
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                value = child.value
                for target in targets:
                    pairs: list[tuple[ast.expr, ast.expr | None]] = [(target, value)]
                    while pairs:
                        part, expression = pairs.pop()
                        if isinstance(part, (ast.Tuple, ast.List)):
                            values = (expression.elts if isinstance(expression, (ast.Tuple, ast.List))
                                      and len(expression.elts) == len(part.elts) else
                                      [None] * len(part.elts))
                            pairs.extend(zip(part.elts, values))
                        elif (isinstance(part, ast.Attribute) and isinstance(part.value, ast.Name)
                              and part.value.id == receiver and part.attr in fields):
                            if isinstance(child, ast.AnnAssign):
                                evidence[part.attr][0].append(
                                    types.annotation(child.annotation, module, locals_))
                            else:
                                inferred = (parameters.get(expression.id, UNKNOWN)
                                            if isinstance(expression, ast.Name) else
                                            types.expression(expression, module, locals_)
                                            if expression is not None else UNKNOWN)
                                if (inferred == UNKNOWN and isinstance(expression, ast.Call)
                                        and isinstance(expression.func, ast.Attribute)
                                        and isinstance(expression.func.value, ast.Name)
                                        and expression.func.value.id == receiver):
                                    method = next((member for member in node.body
                                                   if isinstance(member, (ast.FunctionDef,
                                                                          ast.AsyncFunctionDef))
                                                   and member.name == expression.func.attr
                                                   and member.returns is not None), None)
                                    if method is not None and method.returns is not None:
                                        inferred = types.annotation(method.returns, module)
                                evidence[part.attr][1].append(inferred)
    return evidence
