"""Conservative static type evidence for customer-owned stored fields."""
from __future__ import annotations

import ast
from collections.abc import Set as AbstractSet
from eryx.field_type_analysis.field_type_analysis import EMPTY_NAMES, FieldTypeAnalysis
from eryx.field_type_analysis.evidence_utils import _module, field_evidence

PRIMITIVES = frozenset({"bool", "int", "float", "complex", "str", "bytes"})
BUILTINS = PRIMITIVES | {"list", "dict", "set", "tuple", "frozenset", "object", "type"}
UNKNOWN = "?"


class TypeEvidence(FieldTypeAnalysis):
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
