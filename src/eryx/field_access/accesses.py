"""Resolve access-site bindings against the field ownership index."""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING, override

from eryx.field_access.field_access import FieldAccess
from eryx.field_ownership.bindings import Binding, assigned_names, module_name

if TYPE_CHECKING:
    from eryx.field_ownership.ownership import Ownership


# Keep NodeVisitor first so its traversal wins over the protocol's visit declaration.
class Accesses(ast.NodeVisitor, FieldAccess):
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
        if isinstance(node, ast.Call):
            target = self.value(node.func)
            if target and target[0] == "class":
                return ("instance", target[1])
        return None

    def builtin_call(self, node: ast.Call, name: str) -> bool:
        """Accept unshadowed builtins and explicit imports, not lookalike functions."""
        func = node.func
        if isinstance(func, ast.Name) and func.id == name and all(
                name not in scope for scope in self.scopes):
            return True
        return self.value(func) == ("builtin", name)

    def dictionary_owner(self, node: ast.expr) -> Binding | None:
        if isinstance(node, ast.Attribute) and node.attr == "__dict__":
            return self.value(node.value)
        if (isinstance(node, ast.Call) and self.builtin_call(node, "vars")
                and len(node.args) == 1 and not node.keywords):
            return self.value(node.args[0])
        return None

    def report_field(self, owner: Binding | None, field: str, ctx: ast.expr_context,
                     node: ast.expr, dictionary: bool = False) -> None:
        if owner is None or owner[0] not in {"class", "instance"}:
            return
        declaring = None
        class_field = False
        if owner[0] == "instance":
            if isinstance(ctx, ast.Store):
                declaring = self.ownership.first_write_owner(owner[1], field)
            else:
                declaring = self.ownership.field_owner(owner[1], field)
            class_field = bool(declaring and field in self.ownership.class_fields[declaring]
                               and field not in self.ownership.instance_fields[declaring])
            if dictionary and class_field and not isinstance(ctx, ast.Store):
                declaring = None  # Instance dictionaries do not contain class attributes.
        else:
            declaring = self.ownership.class_field_owner(owner[1], field)
            class_field = declaring is not None
        if declaring and declaring != self.method_owner:
            operation = ("delete" if isinstance(ctx, ast.Del) else
                         "assign" if isinstance(ctx, ast.Store) else "read")
            kind = "class" if class_field else "instance"
            self.diagnostics.append({
                "rule": f"EXTERNAL_{kind.upper()}_FIELD", "path": self.path,
                "line": node.lineno, "column": node.col_offset + 1,
                "symbol": f"{declaring}.{field}",
                "message": f"{operation} of stored {kind} field {declaring}.{field} outside its declaring class."})
        elif owner[0] == "class" and isinstance(ctx, ast.Store):
            self.diagnostics.append({
                "rule": "NO_CLASS_STATE", "path": self.path,
                "line": node.lineno, "column": node.col_offset + 1,
                "symbol": f"{owner[1]}.{field}",
                "message": f"Stored class value {owner[1]}.{field} is prohibited."})

    def annotation(self, node: ast.expr | None, owner: str | None = None) -> Binding | None:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        target = self.value(node)
        if target is None and owner and isinstance(node, ast.Name):
            parts = owner.split(".")
            for size in range(len(parts), 0, -1):
                candidate = ".".join(parts[:size])
                if candidate in self.ownership.stable and parts[size - 1] == node.id:
                    target = ("class", candidate)
                    break
        return ("instance", target[1]) if target and target[0] == "class" else None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.report_field(self.value(node.value), node.attr, node.ctx, node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            self.report_field(self.dictionary_owner(node.value), node.slice.value,
                              node.ctx, node, dictionary=True)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for name, size, context in (("getattr", (2, 3), ast.Load()), ("setattr", (3,), ast.Store()),
                                    ("delattr", (2,), ast.Del())):
            if (len(node.args) in size and not node.keywords and self.builtin_call(node, name)
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                self.report_field(self.value(node.args[0]), node.args[1].value, context, node)
                break
        if isinstance(node.func, ast.Attribute):
            owner = self.dictionary_owner(node.func.value)
            method = node.func.attr
            context: ast.expr_context | None = {
                "get": ast.Load(), "pop": ast.Del(), "setdefault": ast.Store(),
                "__getitem__": ast.Load(), "__setitem__": ast.Store(),
                "__delitem__": ast.Del(),
            }.get(method)
            if (owner and context and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                self.report_field(owner, node.args[0].value, context, node, dictionary=True)
            if (owner and method == "update" and len(node.args) == 1
                    and isinstance(node.args[0], ast.Dict)):
                for key in node.args[0].keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        self.report_field(owner, key.value, ast.Store(), node, dictionary=True)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            module = alias.name if alias.asname else name
            if module == "builtins" and alias.name == "builtins":
                self.scopes[-1][name] = ("builtins", "builtins")
            else:
                self.scopes[-1][name] = ("module", module) if module in self.ownership.modules else None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.module if self.path.endswith("/__init__.py") else self.module.rpartition(".")[0]
            parts = package.split(".") if package else []
            base = ".".join([*parts[:len(parts) - node.level + 1], *([base] if base else [])])
        for alias in node.names:
            source = ("builtins", "builtins") if base == "builtins" and node.level == 0 else ("module", base)
            self.scopes[-1][alias.asname or alias.name] = self.ownership.member(source, alias.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = f"{self.module}.{node.name}" if self.current_class is None else f"{self.current_class}.{node.name}"
        bases: list[str | None] = []
        for base in node.bases:
            target = self.value(base)
            bases.append(target[1] if target and target[0] == "class" else None)
        self.ownership.bases[qualified] = tuple(bases)
        previous = self.current_class
        previous_method = self.method_owner
        self.method_owner = None
        self.current_class = qualified
        # Methods resolve the class name at call time, after its definition is bound.
        self.scopes[-1][node.name] = ("class", qualified) if qualified in self.ownership.stable else None
        # Class namespace is not a lexical scope for methods.
        for statement in node.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(statement)
        self.current_class = previous
        self.method_owner = previous_method
        self.scopes[-1][node.name] = ("class", qualified) if qualified in self.ownership.stable else None

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        owner = self.current_class
        previous_method = self.method_owner
        self.current_class = None
        self.method_owner = None
        locals_: dict[str, Binding | None] = {}
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for arg in args:
            locals_[arg.arg] = self.annotation(arg.annotation, owner)
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

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

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
        inferred = self.value(node.value) if node.value else None
        annotated = self.annotation(node.annotation)
        # An explicit incompatible (or ambiguous) annotation cannot prove ownership.
        if node.value is None or inferred is None or inferred == annotated:
            self.assign(node.target, annotated)
        else:
            self.assign(node.target, None)

    @override
    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.assign(node.name, self.value(node.value))

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

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.assign(item.optional_vars, None)
        for statement in node.body:
            self.visit(statement)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

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

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self.visit_Try(node)

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
