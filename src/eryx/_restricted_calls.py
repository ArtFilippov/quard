"""Declaration evidence and receiver-shaped explicit restricted-call analysis."""
from __future__ import annotations

import ast
from typing import override

from eryx._field_types import UNKNOWN, field_evidence
from eryx._instance_fields_worker import Accesses, Binding, Ownership, module_name
from eryx._markers import FUNCTIONS, local_names


class Restrictions(object):
    """Retain every method declaration rather than collapsing source/stub/overloads."""

    def __init__(self, ownership: Ownership):
        super().__init__()
        self.ownership = ownership
        self.declarations: list[dict[str, object]] = []
        self.methods: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.field_types: dict[tuple[str, str], str] = {}
        for owner, sources in ownership.field_sources.items():
            for path, node, _, _ in sources:
                for member in node.body:
                    if isinstance(member, FUNCTIONS):
                        decorators = member.decorator_list
                        # Properties and binding-changing decorators are later slices.
                        symbols = {ownership.markers.symbol(dec) for dec in decorators
                                   if not ownership.markers.is_identity(dec)}
                        if not symbols <= {"abc.abstractmethod", "typing.override",
                                           "typing.overload", "typing_extensions.override",
                                           "typing_extensions.overload"}:
                            continue
                        evidence: dict[str, object] = {"owner": owner, "member": member.name, "path": path,
                                    "line": member.lineno, "column": member.col_offset + 1,
                                    "marked": any(ownership.markers.is_identity(dec) for dec in decorators)}
                        self.declarations.append(evidence)
                        self.methods.setdefault((owner, member.name), []).append(evidence)
            fields = ownership.fields[owner]
            for field in fields:
                annotations: list[str] = []
                writes: list[str] = []
                for path, node, _, _ in sources:
                    proof = field_evidence(node, {field}, module_name(path),
                                           ownership.type_evidence, ownership.trees[path])[field]
                    annotations.extend(proof[0])
                    writes.extend(proof[1])
                    if field in ownership.class_fields[owner]:
                        for item in node.body:
                            targets = item.targets if isinstance(item, ast.Assign) else (
                                [item.target] if isinstance(item, ast.AnnAssign) else [])
                            if any(isinstance(target, ast.Name) and target.id == field
                                   for target in targets):
                                if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value:
                                    writes.append(ownership.type_evidence.expression(
                                        item.value, module_name(path)))
                proof = annotations or writes
                if proof and UNKNOWN not in proof and len(set(proof)) == 1:
                    self.field_types[owner, field] = proof[0]

    def instance_type(self, qualified: str | None) -> Binding | None:
        if qualified in self.ownership.stable:
            return ("instance", qualified)
        exported = self.ownership.exports.get(qualified or "")
        return ("instance", exported[1]) if exported and exported[0] == "class" else None

    def target(self, owner: str, name: str) -> str | None:
        if owner not in self.ownership.stable:
            return None
        evidence = self.methods.get((owner, name), [])
        return f"{owner}.{name}" if any(item["marked"] for item in evidence) else None

    def field_type(self, owner: str, name: str) -> Binding | None:
        declaring = self.ownership.field_owner(owner, name)
        if declaring is None:
            return None
        qualified = self.field_types.get((declaring, name))
        return self.instance_type(qualified)

    def return_type(self, owner: str, name: str, property_only: bool = False) -> Binding | None:
        node = self.ownership.classes.get(owner)
        if node is None:
            return None
        returns = {self.ownership.type_evidence.annotation(item.returns, owner.rpartition(".")[0])
                   for item in node.body if isinstance(item, FUNCTIONS)
                   and item.name == name and item.returns is not None
                   and (not property_only or any(self.ownership.markers.symbol(dec) in {
                       "builtins.property", "functools.cached_property"} for dec in item.decorator_list))}
        qualified = next(iter(returns)) if len(returns) == 1 else None
        return self.instance_type(qualified)


class Calls(ast.NodeVisitor):
    """Resolve explicit receiver expressions; privilege belongs to their shape."""

    def __init__(self, restrictions: Restrictions, path: str):
        super().__init__()
        self.restrictions = restrictions
        self.resolver = Accesses(restrictions.ownership, path)
        self.current_class: str | None = None
        self.method_owner: str | None = None
        self.prefix = self.resolver.module
        self.receiver: str | None = None
        self.receiver_valid = False
        self.diagnostics: list[dict[str, object]] = []

    def results(self) -> list[dict[str, object]]:
        return self.diagnostics

    def current(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == self.receiver and self.receiver_valid

    def super_owner(self, node: ast.expr) -> str | None:
        if not isinstance(node, ast.Call) or not self.resolver.builtin_call(node, "super"):
            return None
        if node.keywords or self.method_owner is None:
            return None
        if node.args and not (len(node.args) == 2
                              and self.value(node.args[0]) == ("class", self.method_owner)
                              and self.value(node.args[1]) == ("instance", self.method_owner)):
            return None
        bases = self.restrictions.ownership.bases.get(self.method_owner, ())
        return bases[0] if len(bases) == 1 else None

    def value(self, node: ast.expr) -> Binding | None:
        if isinstance(node, ast.Name) and node.id == "super" and all(
                "super" not in scope for scope in self.resolver.scopes):
            return ("builtin", "super")
        if isinstance(node, ast.Attribute):
            owner = self.value(node.value)
            if owner and owner[0] == "instance":
                return (self.restrictions.field_type(owner[1], node.attr)
                        or self.restrictions.return_type(owner[1], node.attr, property_only=True))
            member = self.restrictions.ownership.member(owner, node.attr)
            if member is None and owner and owner[0] == "class":
                declaring = self.restrictions.ownership.class_field_owner(owner[1], node.attr)
                if declaring:
                    return self.restrictions.instance_type(
                        self.restrictions.field_types.get((declaring, node.attr)))
            return member
        if isinstance(node, ast.Call):
            superclass = self.super_owner(node)
            if superclass:
                return ("instance", superclass)
            target = self.value(node.func)
            if target and target[0] == "class":
                return ("instance", target[1])
            if isinstance(node.func, ast.Attribute):
                owner = self.value(node.func.value)
                if owner and owner[0] == "instance":
                    return self.restrictions.return_type(owner[1], node.func.attr)
            return None
        return self.resolver.value(node)

    def allowed(self, node: ast.expr) -> bool:
        if self.current(node):
            return True
        if isinstance(node, ast.Call) and self.super_owner(node):
            return self.receiver_valid and (not node.args or self.current(node.args[1]))
        if isinstance(node, ast.Attribute) and self.current(node.value):
            owner = self.value(node.value)
            if owner:
                declaring = self.restrictions.ownership.field_owner(owner[1], node.attr)
                return bool(declaring and node.attr in
                            self.restrictions.ownership.instance_fields[declaring])
        return False

    @override
    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            owner = self.value(receiver)
            target = self.restrictions.target(owner[1], node.func.attr) if owner else None
            if target and not self.allowed(receiver):
                self.diagnostics.append({
                    "rule": "ENCAPSULATED_ONLY_CALL", "path": self.resolver.path,
                    "line": node.lineno, "column": node.col_offset + 1, "symbol": target,
                    "message": f"Call to restricted method {target} requires the current instance, " +
                               "one direct stored instance field, or proven current-instance super."})
        self.generic_visit(node)

    @override
    def visit_Import(self, node: ast.Import) -> None:
        self.resolver.visit_Import(node)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.resolver.visit_ImportFrom(node)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases, *(item.value for item in node.keywords)):
            self.visit(expression)
        previous = (self.current_class, self.method_owner, self.prefix,
                    self.receiver, self.receiver_valid)
        qualified = f"{self.prefix}.{node.name}"
        self.current_class, self.receiver, self.receiver_valid = qualified, None, False
        self.method_owner, self.prefix = None, qualified
        self.resolver.scopes[-1][node.name] = (("class", qualified) if qualified in
                                             self.restrictions.ownership.stable else None)
        self.resolver.scopes.append({})
        for statement in node.body:
            self.visit(statement)
        _ = self.resolver.scopes.pop()
        (self.current_class, self.method_owner, self.prefix,
         self.receiver, self.receiver_valid) = previous

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        annotations = [arg.annotation for arg in (*node.args.posonlyargs, *node.args.args,
                                                   *node.args.kwonlyargs)]
        annotations.extend(arg.annotation for arg in (node.args.vararg, node.args.kwarg) if arg)
        for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults,
                           *annotations, node.returns):
            if expression is not None:
                self.visit(expression)
        owner = self.current_class
        previous = (self.current_class, self.method_owner, self.prefix,
                    self.receiver, self.receiver_valid)
        locals_: dict[str, Binding | None] = dict.fromkeys(local_names(node))
        args = node.args.posonlyargs + node.args.args
        for arg in (*args, *node.args.kwonlyargs):
            locals_[arg.arg] = self.resolver.annotation(arg.annotation, owner)
        instance = owner is not None and bool(args) and all(
            self.restrictions.ownership.markers.is_identity(dec)
            or self.restrictions.ownership.markers.symbol(dec) in {
                "abc.abstractmethod", "typing.override", "typing_extensions.override",
                "builtins.property", "functools.cached_property"}
            for dec in node.decorator_list)
        self.receiver = args[0].arg if instance else None
        self.receiver_valid = instance
        if instance and owner is not None:
            locals_[args[0].arg] = ("instance", owner)
        class_namespace = self.resolver.scopes.pop() if owner else None
        self.resolver.scopes.append(locals_)
        # Nested functions have no independent instance privilege in this first slice.
        self.current_class = None
        self.method_owner = owner if instance else None
        self.prefix = f"{self.prefix}.{node.name}"
        for statement in node.body:
            self.visit(statement)
        _ = self.resolver.scopes.pop()
        if class_namespace is not None:
            self.resolver.scopes.append(class_namespace)
        (self.current_class, self.method_owner, self.prefix,
         self.receiver, self.receiver_valid) = previous
        self.resolver.scopes[-1][node.name] = None

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def assign(self, target: ast.expr, binding: Binding | None) -> None:
        if isinstance(target, ast.Name) and target.id == self.receiver:
            self.receiver_valid = False
        self.resolver.assign(target, binding)

    @override
    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
            self.assign(target, self.value(node.value))

    @override
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        binding = self.resolver.annotation(node.annotation)
        inferred = self.value(node.value) if node.value is not None else None
        self.assign(node.target, binding if inferred is None or inferred == binding else None)

    @override
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.generic_visit(node)
        self.assign(node.target, None)

    @override
    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            self.assign(target, None)

    @override
    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self.assign(node.target, self.value(node.value))

    @override
    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        before = [scope.copy() for scope in self.resolver.scopes]
        valid = self.receiver_valid
        outcomes: list[tuple[list[dict[str, Binding | None]], bool]] = []
        for branch in (node.body, node.orelse):
            self.resolver.scopes = [scope.copy() for scope in before]
            self.receiver_valid = valid
            for statement in branch:
                self.visit(statement)
            outcomes.append(([scope.copy() for scope in self.resolver.scopes], self.receiver_valid))
        self.resolver.scopes = before
        self.receiver_valid = all(outcome[1] for outcome in outcomes)
        for scope, left, right in zip(before, outcomes[0][0], outcomes[1][0]):
            for name in left.keys() | right.keys():
                scope[name] = left.get(name) if left.get(name) == right.get(name) else None

    @override
    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self.assign(node.target, None)
        self.conditional_body([*node.body, *node.orelse])

    @override
    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def conditional_body(self, statements: list[ast.stmt]) -> None:
        before = [scope.copy() for scope in self.resolver.scopes]
        for statement in statements:
            self.visit(statement)
        after = self.resolver.scopes
        self.resolver.scopes = before
        for scope, updated in zip(before, after):
            for name in scope.keys() | updated.keys():
                if scope.get(name) != updated.get(name):
                    scope[name] = None

    @override
    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.conditional_body([*node.body, *node.orelse])

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Deferred lambda invocation and callable provenance are a later slice.
        pass
