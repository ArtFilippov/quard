"""Report direct access to proven consumer-owned stored instance fields."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

Binding = tuple[str, str]  # (class | instance | module, qualified name)


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


def receiver_fields(node: ast.ClassDef) -> set[str]:
    # Only method receiver stores define fields; deferred nested scopes do not.
    fields: set[str] = set()

    class Stores(ast.NodeVisitor):
        def visit_Attribute(self, attr: ast.Attribute) -> None:
            if (isinstance(attr.ctx, (ast.Store, ast.Del)) and isinstance(attr.value, ast.Name)
                    and attr.value.id == receiver and isinstance(attr.ctx, ast.Store)):
                fields.add(attr.attr)
            self.generic_visit(attr)

        def visit_FunctionDef(self, unused: ast.FunctionDef) -> None:
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    for method in node.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(decorator, ast.Name) and decorator.id in {"classmethod", "staticmethod"}
                   for decorator in method.decorator_list):
                continue
            args = method.args.posonlyargs + method.args.args
            if args:
                receiver = args[0].arg
                for statement in method.body:
                    Stores().visit(statement)
    return fields


class Ownership:
    """Index declarations across all discovered source files before checking uses."""

    def __init__(self, trees: dict[str, ast.Module]):
        self.trees = trees
        self.modules = {module_name(path): path for path in trees}
        self.fields: dict[str, set[str]] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        for path, tree in trees.items():
            self.index(tree.body, module_name(path))

    def index(self, body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = f"{prefix}.{node.name}"
                self.classes[name] = node
                methods = {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
                declared = {target.id for item in node.body for target in (
                    item.targets if isinstance(item, ast.Assign) else
                    [item.target] if isinstance(item, ast.AnnAssign) else [])
                    if isinstance(target, ast.Name)}
                properties = {target.id for item in node.body if isinstance(item, ast.Assign)
                              and isinstance(item.value, ast.Call)
                              and isinstance(item.value.func, ast.Name) and item.value.func.id == "property"
                              for target in item.targets if isinstance(target, ast.Name)}
                self.fields[name] = (receiver_fields(node) | declared) - methods - properties
                self.index(node.body, name)

    def member(self, owner: Binding | None, name: str) -> Binding | None:
        if owner is None:
            return None
        kind, qualified = owner
        if kind == "module":
            candidate = f"{qualified}.{name}"
            if candidate in self.modules:
                return ("module", candidate)
            if candidate in self.classes and not self.classes[candidate].decorator_list:
                return ("class", candidate)
        if (kind == "class" and f"{qualified}.{name}" in self.classes
                and not self.classes[f"{qualified}.{name}"].decorator_list):
            return ("class", f"{qualified}.{name}")
        return None


class Accesses(ast.NodeVisitor):
    """Follow local, explicit bindings; unknown or reassigned values remain unknown."""

    def __init__(self, ownership: Ownership, path: str):
        self.ownership = ownership
        self.path = path
        self.module = module_name(path)
        self.scopes: list[dict[str, Binding | None]] = [{}]
        self.current_class: str | None = None
        self.method_owner: str | None = None
        self.diagnostics: list[dict[str, object]] = []

    def results(self) -> list[dict[str, object]]:
        return self.diagnostics

    def binding(self, name: str) -> Binding | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def value(self, node: ast.expr) -> Binding | None:
        if isinstance(node, ast.Name):
            return self.binding(node.id)
        if isinstance(node, ast.Attribute):
            return self.ownership.member(self.value(node.value), node.attr)
        if isinstance(node, ast.Call) and not node.keywords:
            target = self.value(node.func)
            if target and target[0] == "class":
                return ("instance", target[1])
        return None

    def annotation(self, node: ast.expr | None) -> Binding | None:
        if node is None:
            return None
        target = self.value(node)
        return ("instance", target[1]) if target and target[0] == "class" else None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        owner = self.value(node.value)
        if (owner and owner[0] == "instance" and owner[1] != self.method_owner
                and node.attr in self.ownership.fields[owner[1]]):
            operation = ("delete" if isinstance(node.ctx, ast.Del) else
                         "assign" if isinstance(node.ctx, ast.Store) else "read")
            self.diagnostics.append({
                "rule": "EXTERNAL_INSTANCE_FIELD", "path": self.path,
                "line": node.lineno, "column": node.col_offset + 1,
                "symbol": f"{owner[1]}.{node.attr}",
                "message": f"{operation} of stored instance field {owner[1]}.{node.attr} outside its declaring class."})
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            module = alias.name if alias.asname else name
            self.scopes[-1][name] = ("module", module) if module in self.ownership.modules else None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.module if self.path.endswith("/__init__.py") else self.module.rpartition(".")[0]
            parts = package.split(".") if package else []
            base = ".".join([*parts[:len(parts) - node.level + 1], *([base] if base else [])])
        for alias in node.names:
            self.scopes[-1][alias.asname or alias.name] = self.ownership.member(("module", base), alias.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = f"{self.module}.{node.name}" if self.current_class is None else f"{self.current_class}.{node.name}"
        previous = self.current_class
        previous_method = self.method_owner
        self.method_owner = None
        self.current_class = qualified
        # Class namespace is not a lexical scope for methods.
        for statement in node.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(statement)
        self.current_class = previous
        self.method_owner = previous_method
        self.scopes[-1][node.name] = (("class", qualified)
                                      if qualified in self.ownership.classes and not node.decorator_list else None)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        owner = self.current_class
        previous_method = self.method_owner
        self.current_class = None
        self.method_owner = None
        locals_: dict[str, Binding | None] = {}
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in args:
            locals_[arg.arg] = self.annotation(arg.annotation)
        if owner in self.ownership.classes and args and not any(
                isinstance(decorator, ast.Name) and decorator.id in {"classmethod", "staticmethod"}
                for decorator in node.decorator_list):
            locals_[args[0].arg] = ("instance", owner)
        if node.args.vararg:
            locals_[node.args.vararg.arg] = None
        if node.args.kwarg:
            locals_[node.args.kwarg.arg] = None
        # Function locals shadow outer names throughout the body, even before assignment.
        for statement in node.body:
            for item in ast.walk(statement):
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                    locals_.setdefault(item.id, None)
        self.scopes.append(locals_)
        # Accesses in a directly declared method are within its declaring class.
        self.method_owner = owner or previous_method
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()
        self.current_class = owner
        self.method_owner = previous_method
        self.scopes[-1][node.name] = None

    visit_AsyncFunctionDef = visit_FunctionDef

    def assign(self, target: ast.expr, value: Binding | None) -> None:
        if isinstance(target, ast.Name):
            self.scopes[-1][target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.assign(item, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = self.value(node.value)
        for target in node.targets:
            self.visit(target)
            self.assign(target, value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        self.visit(node.target)
        self.assign(node.target, self.value(node.value) if node.value else self.annotation(node.annotation))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self.assign(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self.assign(node.target, self.value(node.value))

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            self.assign(target, None)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        before = [scope.copy() for scope in self.scopes]
        self.assign(node.target, None)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        after = [scope.copy() for scope in self.scopes]
        self.scopes = before
        for scope, updated in zip(self.scopes, after):
            for name in updated.keys() | scope.keys():
                if updated.get(name) != scope.get(name) or name in assigned_names(node.target):
                    scope[name] = None

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.assign(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        before = [scope.copy() for scope in self.scopes]
        outcomes = []
        for branch in (node.body, node.orelse):
            for statement in branch:
                self.visit(statement)
            outcomes.append([scope.copy() for scope in self.scopes])
            self.scopes = [scope.copy() for scope in before]
        for scope, left, right in zip(self.scopes, *outcomes):
            for name in left.keys() | right.keys():
                scope[name] = left.get(name) if left.get(name) == right.get(name) else None

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        for name in assigned_names(node):
            self.scopes[-1][name] = None

    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        before = [scope.copy() for scope in self.scopes]
        for statement in node.body:
            self.visit(statement)
        body = [scope.copy() for scope in self.scopes]
        for handler in node.handlers:
            self.scopes = [scope.copy() for scope in before]
            for name in assigned_names(node):
                self.scopes[-1][name] = None
            if handler.name:
                self.scopes[-1][handler.name] = None
            for statement in handler.body:
                self.visit(statement)
        self.scopes = body
        for statement in (*node.orelse, *node.finalbody):
            self.visit(statement)
        for name in assigned_names(node):
            self.scopes[-1][name] = None

    visit_TryStar = visit_Try

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        before = [scope.copy() for scope in self.scopes]
        for case in node.cases:
            self.scopes = [scope.copy() for scope in before]
            for name in assigned_names(case.pattern):
                self.scopes[-1][name] = None
            if case.guard:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
        self.scopes = before
        for name in assigned_names(node):
            self.scopes[-1][name] = None


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid instance field source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "INSTANCE_FIELDS_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
    ownership = Ownership(trees)
    for path, tree in trees.items():
        visitor = Accesses(ownership, path)
        visitor.visit(tree)
        diagnostics.extend(visitor.results())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Instance field manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "INSTANCE_FIELDS_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"eryx_instance_fields_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
