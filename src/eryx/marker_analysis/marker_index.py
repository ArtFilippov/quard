"""Binding-aware recognition of consumer-supplied identity markers."""
from __future__ import annotations

import ast
from pathlib import Path

from eryx.marker_analysis.marker_analysis import MarkerAnalysis

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
MARKER = "<encapsulated_only>"


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


class MarkerIndex(MarkerAnalysis):
    """Keep per-declaration decorators, including separate source/stub evidence."""

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__()
        from eryx.marker_binding_analysis.marker_bindings import MarkerBindings

        self.modules = {module_name(path) for path in trees}
        self.exports: dict[str, str | None] = {}
        self.decorators: dict[ast.expr, str | None] = {}
        self.descriptor_assignments: dict[ast.stmt, str] = {}
        for _ in range(len(trees) + 2):
            previous = self.exports.copy()
            self.decorators.clear()
            self.descriptor_assignments.clear()
            for path, tree in trees.items():
                bindings = MarkerBindings(self, path)
                bindings.suite(tree.body)
                for name, value in bindings.scopes[0].items():
                    if "." not in name:
                        self.exports[f"{module_name(path)}.{name}"] = value
            if self.exports == previous:
                break

    def is_identity(self, node: ast.expr) -> bool:
        return self.decorators.get(node) == MARKER

    def symbol(self, node: ast.expr) -> str | None:
        return self.decorators.get(node)

    def property_declarations(self, node: ast.ClassDef) -> dict[str, list[tuple[ast.stmt, str, str]]]:
        """Return proven descriptor declarations without collapsing accessors."""
        result: dict[str, list[tuple[ast.stmt, str, str]]] = {}
        for item in node.body:
            if isinstance(item, FUNCTIONS):
                for dec in item.decorator_list:
                    symbol = self.symbol(dec)
                    if symbol in {"builtins.property", "functools.cached_property"}:
                        result.setdefault(item.name, []).append((item, "getter", symbol))
                    elif (symbol in {"builtins.property.setter", "builtins.property.deleter",
                                      "builtins.property.getter"} and isinstance(dec, ast.Attribute)
                          and isinstance(dec.value, ast.Name)):
                        result.setdefault(dec.value.id, []).append((item, dec.attr, "builtins.property"))
            elif isinstance(item, (ast.Assign, ast.AnnAssign)) and item in self.descriptor_assignments:
                kind = self.descriptor_assignments[item]
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        result.setdefault(target.id, []).append((item, "constructor", kind))
        return result

    def imported(self, qualified: str) -> str | None:
        if qualified in self.exports:
            return self.exports[qualified]
        # External marker definitions are opaque; the explicit import is the convention.
        roots = {module.split(".")[0] for module in self.modules}
        if qualified.split(".")[0] not in roots and qualified.endswith(".encapsulated_only"):
            return MARKER
        return qualified
