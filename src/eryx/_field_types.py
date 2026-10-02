"""Conservative static type evidence for customer-owned stored fields."""
from __future__ import annotations

import ast
from collections.abc import Set as AbstractSet
from pathlib import Path

PRIMITIVES = frozenset({"bool", "int", "float", "complex", "str", "bytes"})
BUILTINS = PRIMITIVES | {"list", "dict", "set", "tuple", "frozenset", "object", "type"}
UNKNOWN = "?"
EMPTY_NAMES: frozenset[str] = frozenset()


def _module(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class TypeEvidence:
    """Resolve annotations and uncomplicated expressions without runtime inference."""

    def __init__(self, trees: dict[str, ast.Module]):
        self.trees = trees
        self.imports: dict[str, dict[str, str]] = {}
        self.definitions: dict[str, dict[str, ast.AST]] = {}
        for path, tree in trees.items():
            module = _module(path)
            imports: dict[str, str] = {}
            definitions: dict[str, ast.AST] = {}
            for item in tree.body:
                if isinstance(item, ast.Import):
                    for alias in item.names:
                        imports[alias.asname or alias.name.split(".")[0]] = (
                            alias.name if alias.asname else alias.name.split(".")[0])
                elif isinstance(item, ast.ImportFrom) and item.module:
                    base = item.module
                    if item.level:
                        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
                        base = ".".join(filter(None, [".".join(package.split(".")[:
                                                     len(package.split(".")) - item.level + 1]), base]))
                    for alias in item.names:
                        imports[alias.asname or alias.name] = f"{base}.{alias.name}"
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    definitions[item.name] = item
                    imports.pop(item.name, None)
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            imports.pop(target.id, None)
                            definitions[target.id] = item
                elif isinstance(item, ast.TypeAlias) and isinstance(item.name, ast.Name):
                    imports.pop(item.name.id, None)
                    definitions[item.name.id] = item
            self.imports[module] = imports
            self.definitions[module] = definitions

    def name(self, node: ast.expr, module: str, local: AbstractSet[str] = EMPTY_NAMES,
             seen: frozenset[str] = EMPTY_NAMES) -> str:
        if isinstance(node, ast.Name):
            if node.id in local:
                return UNKNOWN
            if node.id in self.imports.get(module, {}):
                return self.imports[module][node.id]
            if node.id in self.definitions.get(module, {}):
                qualified = f"{module}.{node.id}"
                definition = self.definitions[module][node.id]
                if qualified not in seen:
                    if isinstance(definition, ast.TypeAlias):
                        return self.name(definition.value, module, local, seen | {qualified})
                    if (isinstance(definition, ast.Assign) and len(definition.targets) == 1
                            and isinstance(definition.targets[0], ast.Name)):
                        return self.name(definition.value, module, local, seen | {qualified})
                return qualified
            if node.id in BUILTINS:
                return f"builtins.{node.id}"
            if node.id == "None":
                return "builtins.NoneType"
            return UNKNOWN
        if isinstance(node, ast.Attribute):
            base = self.name(node.value, module, local, seen)
            return f"{base}.{node.attr}" if base != UNKNOWN else UNKNOWN
        return UNKNOWN

    def annotation(self, node: ast.expr, module: str,
                   local: AbstractSet[str] = EMPTY_NAMES) -> str:
        if isinstance(node, ast.Constant):
            if node.value is None:
                return "builtins.NoneType"
            if isinstance(node.value, str):
                try:
                    return self.annotation(ast.parse(node.value, mode="eval").body, module, local)
                except SyntaxError:
                    return UNKNOWN
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = self.annotation(node.left, module, local)
            right = self.annotation(node.right, module, local)
            return f"union[{left},{right}]" if UNKNOWN not in {left, right} else UNKNOWN
        if isinstance(node, ast.Subscript):
            origin = self.name(node.value, module, local)
            arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            resolved = [self.annotation(argument, module, local) for argument in arguments]
            return (f"{origin}[{','.join(resolved)}]" if origin not in {UNKNOWN, "typing.Any",
                                                                                 "typing_extensions.Any"}
                    and UNKNOWN not in resolved else UNKNOWN)
        name = self.name(node, module, local)
        return UNKNOWN if name in {"typing.Any", "typing_extensions.Any"} else name

    def expression(self, node: ast.expr, module: str,
                   local: AbstractSet[str] = EMPTY_NAMES) -> str:
        if isinstance(node, ast.Constant):
            return f"builtins.{type(node.value).__name__}"
        if isinstance(node, ast.JoinedStr):
            return "builtins.str"
        if isinstance(node, (ast.List, ast.ListComp)):
            return "builtins.list"
        if isinstance(node, (ast.Dict, ast.DictComp)):
            return "builtins.dict"
        if isinstance(node, (ast.Set, ast.SetComp)):
            return "builtins.set"
        if isinstance(node, (ast.Tuple, ast.GeneratorExp)):
            return "builtins.tuple" if isinstance(node, ast.Tuple) else UNKNOWN
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return self.expression(node.operand, module, local)
        if isinstance(node, ast.Call):
            target = self.name(node.func, module, local)
            if target in {f"builtins.{name}" for name in BUILTINS}:
                return target
            parent, _, function = target.rpartition(".")
            definition = self.definitions.get(parent, {}).get(function)
            if isinstance(definition, ast.ClassDef):
                return target
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) and definition.returns:
                return self.annotation(definition.returns, parent)
        return UNKNOWN


def field_evidence(node: ast.ClassDef, fields: set[str], module: str,
                   types: TypeEvidence) -> dict[str, tuple[list[str], list[str]]]:
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
            elif (isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Attribute)
                  and isinstance(child.target.value, ast.Name) and child.target.value.id == receiver
                  and child.target.attr in fields):
                evidence[child.target.attr][1].append(UNKNOWN)
    return evidence
