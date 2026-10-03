"""Binding-aware recognition of consumer-supplied identity markers."""
from __future__ import annotations

import ast
from pathlib import Path

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


class MarkerIndex(object):
    """Keep per-declaration decorators, including separate source/stub evidence."""

    def __init__(self, trees: dict[str, ast.Module]):
        super().__init__()
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


class MarkerBindings(object):
    """Resolve decorator expressions at declaration time, respecting lexical binding."""

    def __init__(self, index: MarkerIndex, path: str):
        super().__init__()
        self.index = index
        self.path = path
        self.scopes: list[dict[str, str | None]] = [{}]
        self.class_scope = False

    def value(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            for scope in reversed(self.scopes):
                if node.id in scope:
                    return scope[node.id]
            return f"builtins.{node.id}" if node.id in {"property", "staticmethod", "classmethod"} else None
        if isinstance(node, ast.Attribute):
            owner = self.value(node.value)
            if owner and owner != MARKER:
                qualified = f"{owner}.{node.attr}"
                return self.imported(qualified)
        return None

    def imported(self, qualified: str) -> str | None:
        for scope in reversed(self.scopes):
            if qualified in scope:
                return scope[qualified]
        return self.index.imported(qualified)

    def assign(self, target: ast.expr, value: str | None) -> None:
        if isinstance(target, ast.Name):
            self.scopes[-1][target.id] = value
        elif isinstance(target, ast.Attribute):
            owner = self.value(target.value)
            if owner and owner != MARKER:
                self.scopes[-1][f"{owner}.{target.attr}"] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.assign(item, None)
        elif isinstance(target, ast.Starred):
            self.assign(target.value, None)

    def suite(self, body: list[ast.stmt], conditional: bool = False) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name.split(".")[0]] = (
                        None if conditional else alias.name if alias.asname else alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.scopes[-1][alias.asname or alias.name] = (None if conditional else
                        self.imported(f"{import_base(self.path, node)}.{alias.name}"))
            elif isinstance(node, FUNCTIONS + (ast.ClassDef,)):
                for decorator in node.decorator_list:
                    self.index.decorators[decorator] = self.value(decorator)
                descriptor = next((self.value(dec) for dec in node.decorator_list
                                   if self.value(dec) in {"builtins.property", "functools.cached_property",
                                                         "builtins.property.setter", "builtins.property.deleter",
                                                         "builtins.property.getter"}), None)
                previous = self.class_scope
                class_namespace = self.scopes.pop() if previous and isinstance(node, FUNCTIONS) else None
                self.scopes.append(dict.fromkeys(local_names(node)) if isinstance(node, FUNCTIONS) else {})
                self.class_scope = isinstance(node, ast.ClassDef)
                self.suite(node.body)
                _ = self.scopes.pop()
                if class_namespace is not None:
                    self.scopes.append(class_namespace)
                self.class_scope = previous
                self.scopes[-1][node.name] = (MARKER if not conditional and isinstance(node, FUNCTIONS)
                                              and identity_definition(node, self.path) else
                                              "builtins.property" if not conditional and descriptor and
                                              descriptor.startswith("builtins.property") else
                                              descriptor if not conditional else None)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                if isinstance(node.value, ast.Call) and not conditional:
                    kind = self.value(node.value.func)
                    if kind in {"builtins.property", "functools.cached_property"}:
                        self.index.descriptor_assignments[node] = kind
                value = self.value(node.value) if node.value is not None and not conditional else None
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    self.assign(target, self.index.descriptor_assignments.get(node, value))
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, ast.Delete) else [node.target]
                for target in targets:
                    self.assign(target, None)
            else:
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    self.assign(node.target, None)
                if isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if item.optional_vars is not None:
                            self.assign(item.optional_vars, None)
                # Conditional bindings cannot prove a marker, including after a branch.
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        self.suite([child], True)
                    elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                        if isinstance(child, ast.ExceptHandler) and child.name:
                            self.scopes[-1][child.name] = None
                        if isinstance(child, ast.match_case):
                            for pattern in ast.walk(child.pattern):
                                if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
                                    self.scopes[-1][pattern.name] = None
                        self.suite(child.body, True)
