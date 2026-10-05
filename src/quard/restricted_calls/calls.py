"""Receiver/callable provenance and call-site analysis for restricted calls."""
from __future__ import annotations

import ast
from typing import override

from quard.dispatch_site_model.dispatch_site import DispatchSite
from quard.field_access.accesses import Accesses
from quard.field_ownership.bindings import Binding, PROTOCOL_BUILTINS, assigned_names
from quard.field_ownership.ownership import Ownership
from quard.marker_analysis.marker_index import local_names
from quard.restricted_calls.restricted_calls import RestrictedCalls
from quard.restricted_targets.restricted_targets import Parents, RestrictedTargets


_BINARY = {
    ast.Add: ("__add__", "__radd__", "__iadd__"),
    ast.Sub: ("__sub__", "__rsub__", "__isub__"),
    ast.Mult: ("__mul__", "__rmul__", "__imul__"),
    ast.MatMult: ("__matmul__", "__rmatmul__", "__imatmul__"),
    ast.Div: ("__truediv__", "__rtruediv__", "__itruediv__"),
    ast.FloorDiv: ("__floordiv__", "__rfloordiv__", "__ifloordiv__"),
    ast.Mod: ("__mod__", "__rmod__", "__imod__"),
    ast.Pow: ("__pow__", "__rpow__", "__ipow__"),
    ast.LShift: ("__lshift__", "__rlshift__", "__ilshift__"),
    ast.RShift: ("__rshift__", "__rrshift__", "__irshift__"),
    ast.BitAnd: ("__and__", "__rand__", "__iand__"),
    ast.BitOr: ("__or__", "__ror__", "__ior__"),
    ast.BitXor: ("__xor__", "__rxor__", "__ixor__"),
}
_COMPARE = {ast.Lt: ("__lt__", "__gt__"), ast.LtE: ("__le__", "__ge__"),
            ast.Eq: ("__eq__", "__eq__"), ast.NotEq: ("__ne__", "__ne__"),
            ast.Gt: ("__gt__", "__lt__"), ast.GtE: ("__ge__", "__le__")}
_UNARY = {ast.UAdd: "__pos__", ast.USub: "__neg__", ast.Invert: "__invert__"}
_BUILTIN_DIRECT = {"len": "__len__", "repr": "__repr__", "ascii": "__repr__",
                   "bin": "__index__", "oct": "__index__",
                   "hex": "__index__", "abs": "__abs__", "round": "__round__",
                   "hash": "__hash__"}
_BUILTIN_FALLBACK = {"str": ("__str__", "__repr__"),
                     "int": ("__int__", "__index__"),
                     "float": ("__float__", "__index__"),
                     "complex": ("__complex__", "__float__", "__index__")}
_BUILTIN_ITERATION = {"iter", "next", "list", "tuple", "any", "all"}
_BUILTIN_OPTIONAL_ARGUMENT = {"format", "round", "next"}


# Keep NodeVisitor first so its traversal wins over the protocol's visit declaration.
class Calls(ast.NodeVisitor, RestrictedCalls):
    """Resolve explicit calls while keeping identity separate from known types."""

    def __init__(self, restrictions: RestrictedTargets, path: str,
                 ownership: Ownership,
                 *, parents: Parents | None = None):
        super().__init__()
        self.restrictions = restrictions
        self.resolver = Accesses(ownership, path)
        self._context: dict[str, str | None] = {
            "current_class": None, "method_owner": None, "receiver": None}
        self.prefix: str = self.resolver.module
        self.receiver_valid: bool = False
        self.diagnostics: list[dict[str, object]] = []
        # Provenance is independent of the type bindings in Accesses: a peer and
        # the current instance can have the same type but different permissions.
        self.provenance: list[dict[str, frozenset[Binding]]] = [{}]
        self._assigned_origin: set[Binding] = set()
        self._operator_seen: set[tuple[int, int, str]] = set()
        self._operator_results: dict[int, frozenset[Binding]] = {}
        self._reassigned_builtins: set[str] = set()
        self._ancestry_parents: Parents | None = parents

    def _parent_map(self) -> dict[str, tuple[str | None, ...]]:
        """Return the ancestry map used when this visitor collects class bases."""
        assert self._ancestry_parents is not None
        return self._ancestry_parents

    @override
    def results(self) -> list[dict[str, object]]:
        return self.diagnostics

    def current(self, node: ast.expr) -> bool:
        owner = self._context["method_owner"]
        return self.origins(node) == frozenset({("current", owner)}) if owner else False

    def origins(self, node: ast.expr) -> frozenset[Binding]:
        if isinstance(node, (ast.BinOp, ast.UnaryOp, ast.Compare)):
            return self._operator_results.get(id(node), frozenset())
        if isinstance(node, ast.BoolOp):
            return frozenset().union(*(self.origins(value) for value in node.values))
        if isinstance(node, ast.Name):
            for scope in reversed(self.provenance):
                if node.id in scope:
                    if scope[node.id]:
                        return scope[node.id]
                    break
            binding = self.value(node)
            return frozenset({binding}) if binding else frozenset()
        if isinstance(node, ast.IfExp):
            return self.origins(node.body) | self.origins(node.orelse)
        if isinstance(node, ast.Attribute):
            result: set[Binding] = set()
            for kind, owner in self.origins(node.value):
                if kind in {"instance", "current", "field"}:
                    target = self.restrictions.target(owner, node.attr)
                    if target:
                        result.add(("method", target))
                    field = self.restrictions.field_type(owner, node.attr)
                    if field:
                        result.add(("field", field[1]))
                    result.update(self._other_origin(owner, node.attr))
                elif kind == "class":
                    target = self.restrictions.target(owner, node.attr)
                    if target:
                        result.add(("method", target))
                    if field := self.restrictions.class_field_type(owner, node.attr):
                        result.add(field)
            if not result:
                binding = self.value(node)
                if binding:
                    result.add(binding)
            return frozenset(result)
        if isinstance(node, ast.Subscript):
            return frozenset(returned for kind, owner in self.origins(node.value)
                             if kind in {"instance", "current", "field"}
                             if (returned := self.restrictions.return_type(owner, "__getitem__")))
        if isinstance(node, ast.Call):
            if self.builtin_call(node, "iter") and len(node.args) == 1 and not node.keywords:
                return self.iterator_origins(node.args[0])
            if (self.resolver.builtin_call(node, "getattr") and len(node.args) == 2
                    and not node.keywords and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                synthetic = ast.Attribute(value=node.args[0], attr=node.args[1].value, ctx=ast.Load())
                return self.origins(synthetic)
            if self.super_owner(node):
                superclass = self.super_owner(node)
                return frozenset({("instance", superclass)}) if superclass else frozenset()
            target = self.value(node)
            if target:
                return frozenset({target})
            return frozenset(returned for kind, owner in self.origins(node.func)
                             if kind in {"instance", "current", "field"}
                             if (returned := self.restrictions.return_type(owner, "__call__")))
        binding = self.value(node)
        return frozenset({binding}) if binding else frozenset()

    def _other_origin(self, owner: str, name: str) -> set[Binding]:
        result: set[Binding] = set()
        returned = self.restrictions.return_type(owner, name, property_only=True)
        if returned:
            result.add(returned)
        return result

    def owners(self, node: ast.expr) -> set[str]:
        return {owner for kind, owner in self.origins(node)
                if kind in {"instance", "current", "field", "class"}}

    def targets(self, node: ast.expr) -> set[str]:
        if isinstance(node, ast.Attribute):
            return {target for owner in self.owners(node.value)
                    if (target := self.restrictions.target(owner, node.attr))}
        if (isinstance(node, ast.Call) and len(node.args) == 2 and not node.keywords
                and self.resolver.builtin_call(node, "getattr")
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            return {target for owner in self.owners(node.args[0])
                    if (target := self.restrictions.target(owner, node.args[1].value))}
        return {target for kind, target in self.origins(node) if kind == "method"}

    def super_owner(self, node: ast.expr) -> str | None:
        if not isinstance(node, ast.Call) or not self.resolver.builtin_call(node, "super"):
            return None
        owner = self._context["method_owner"]
        if node.keywords or owner is None:
            return None
        if not node.args and (self._context["receiver"] is None or not self.receiver_valid):
            return None
        if node.args and not (len(node.args) == 2
                              and self.value(node.args[0]) == ("class", owner)
                              and self.value(node.args[1]) == ("instance", owner)):
            return None
        return self.restrictions.direct_parent(owner)

    def value(self, node: ast.expr) -> Binding | None:
        if isinstance(node, ast.Name) and node.id in PROTOCOL_BUILTINS | {
                "super", "getattr", "setattr", "delattr", "vars", "bool"} and all(
                node.id not in scope for scope in self.resolver.scopes):
            return ("builtin", node.id)
        if isinstance(node, ast.Name):
            for scope in reversed(self.provenance):
                if node.id in scope:
                    types = {(kind if kind in {"class", "module", "builtin", "builtins"} else "instance", owner)
                             for kind, owner in scope[node.id] if kind != "method"}
                    if len(types) == 1:
                        return next(iter(types))
                    if len(types) > 1:
                        return None
                    break
        if isinstance(node, ast.Attribute):
            owner = self.value(node.value)
            if (owner == ("builtins", "builtins") and node.attr in self._reassigned_builtins):
                return None
            if owner and owner[0] == "instance":
                return (self.restrictions.field_type(owner[1], node.attr)
                        or self.restrictions.return_type(owner[1], node.attr, property_only=True))
            member = self.restrictions.member(owner, node.attr)
            if member is None and owner and owner[0] == "class":
                return self.restrictions.class_field_type(owner[1], node.attr)
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

    def builtin_call(self, node: ast.Call, name: str) -> bool:
        """Prove a helper binding without borrowing a replaced builtins member.

        Args:
            node (ast.Call): Candidate invocation.
            name (str): Expected builtin.

        Returns:
            bool: Whether the callee still names that builtin.
        """
        if not self.resolver.builtin_call(node, name) or self.value(node.func) != ("builtin", name):
            return False
        return not (name in self._reassigned_builtins and isinstance(node.func, ast.Name)
                    and node.func.id == name and all(name not in scope for scope in self.resolver.scopes))

    def iterator_origins(self, receiver: ast.expr) -> frozenset[Binding]:
        """Keep original identity only for a proven direct return of self.

        Args:
            receiver (ast.expr): Expression passed to iter.

        Returns:
            frozenset[Binding]: Possible resulting iterator origins.
        """
        return frozenset(
            (kind, owner) if self.restrictions.protocol_returns_self(owner, "__iter__")
            else returned
            for kind, owner in self.origins(receiver) if kind in {"instance", "current", "field"}
            if (returned := self.restrictions.protocol_return_type(owner, "__iter__")))

    def allowed(self, node: ast.expr) -> bool:
        if self.current(node):
            return True
        if (isinstance(node, ast.Call) and self.builtin_call(node, "iter")
                and len(node.args) == 1 and not node.keywords and self.iterator_origins(node.args[0])
                and all(self.restrictions.protocol_returns_self(owner, "__iter__")
                        for kind, owner in self.origins(node.args[0])
                        if kind in {"instance", "current", "field"})):
            return self.allowed(node.args[0])
        if isinstance(node, ast.Call) and self.super_owner(node):
            return self.current(node.args[1]) if node.args else self.receiver_valid
        if isinstance(node, ast.Attribute) and self.current(node.value):
            owner = self.value(node.value)
            if owner:
                return self.restrictions.direct_field(owner[1], node.attr)
        return False

    def property_access(self, receiver: ast.expr, name: str, node: ast.expr,
                         operation: str) -> None:
        for owner in sorted(self.owners(receiver)):
            target = self.restrictions.property_target(owner, name)
            if target and (not self.allowed(receiver) or any(
                    kind == "class" for kind, _ in self.origins(receiver))):
                self.diagnostics.append({
                    "rule": "ENCAPSULATED_ONLY_PROPERTY", "path": self.resolver.path,
                    "line": node.lineno, "column": node.col_offset + 1, "symbol": target,
                    "message": f"{operation.capitalize()} of restricted property {target} requires "
                               + "the current instance, one direct stored instance field, "
                                + "or proven current-instance super."})

    def dispatch_site(self, node: ast.expr, receiver: ast.expr,
                       names: tuple[str, ...]) -> DispatchSite:
        origins = self.origins(receiver)
        candidates = {target for kind, owner in origins if kind in {"instance", "current", "field"}
                      for name in names if (target := self.restrictions.special_target(owner, name))}
        return DispatchSite(node, receiver, origins, tuple(sorted(candidates)))

    def operand_origins(self, node: ast.expr) -> frozenset[Binding]:
        origins = self.origins(node)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float, str, bytes, bool, complex}:
            return frozenset({("builtin", type(node.value).__name__)})
        return origins

    def operator_call(self, node: ast.expr, receiver: ast.expr,
                      origin: Binding, name: str) -> None:
        if origin[0] not in {"instance", "current", "field"}:
            return
        target = self.restrictions.special_target(origin[1], name)
        if target:
            forbidden = (origin[0] == "instance" or origin[0] == "field"
                         and not self.direct_field_origin(receiver, origin))
            if not forbidden:
                return
            key = (id(node), id(receiver), target)
            if key in self._operator_seen:
                return
            self._operator_seen.add(key)
            self.implicit_call(DispatchSite(node, receiver, frozenset({origin}), (target,)),
                               intermediate=True)

    def direct_field_origin(self, receiver: ast.expr, origin: Binding) -> bool:
        if isinstance(receiver, ast.IfExp):
            return any(origin in self.origins(branch) and self.direct_field_origin(branch, origin)
                       for branch in (receiver.body, receiver.orelse))
        if isinstance(receiver, ast.BoolOp):
            return any(origin in self.origins(value) and self.direct_field_origin(value, origin)
                       for value in receiver.values)
        return self.allowed(receiver)

    def binary_dispatch(self, node: ast.expr, left: ast.expr, right: ast.expr,
                        normal: str, reflected: str, inplace: str | None = None,
                        left_evidence: frozenset[Binding] | None = None
                        ) -> frozenset[Binding]:
        """Follow attempted slots; a proven value stops subsequent alternatives."""
        left_origins = left_evidence if left_evidence is not None else self.operand_origins(left)
        right_origins = self.operand_origins(right)
        results: set[Binding] = set()
        def attempt(receiver: ast.expr, origin: Binding, name: str) -> str:
            original = name
            name = self.restrictions.comparison_method(origin[1], name)
            self.operator_call(node, receiver, origin, name)
            outcome = self.restrictions.method_outcome(origin[1], name)
            if outcome != "fallback" and (returned := self.restrictions.protocol_return_type(
                    origin[1], name)):
                if original != name:
                    self.truth_results(node, frozenset({returned}))
                else:
                    results.add(returned)
            return outcome
        for left_origin in left_origins:
            if left_origin[0] not in {"instance", "current", "field", "builtin"}:
                continue
            # An unresolved right operand does not establish a reflected target.
            pairs = right_origins or frozenset({("unknown", "")})
            for right_origin in pairs:
                left_owner, right_owner = left_origin[1], right_origin[1]
                right_name = self.restrictions.comparison_method(right_owner, reflected)
                left_name = self.restrictions.comparison_method(left_owner, reflected)
                early = (right_origin[0] in {"instance", "current", "field"}
                         and left_owner != right_owner
                         and left_owner in (self.restrictions.mro(right_owner) or [])[1:]
                         and self.restrictions.special_method(right_owner, right_name) is not None
                         and self.restrictions.special_method(right_owner, right_name) !=
                             self.restrictions.special_method(left_owner, left_name))
                if inplace:
                    if attempt(left, left_origin, inplace) == "value":
                        continue
                if early:
                    if attempt(right, right_origin, reflected) == "value":
                        continue
                if attempt(left, left_origin, normal) == "value":
                    continue
                if (not early and right_origin[0] in {"instance", "current", "field"}
                        and left_owner != right_owner):
                    _ = attempt(right, right_origin, reflected)
        return frozenset(results)

    def truth(self, node: ast.expr) -> None:
        if isinstance(node, ast.BoolOp):
            self.truth(node.values[-1])
            return
        if isinstance(node, ast.IfExp):
            self.truth(node.body)
            self.truth(node.orelse)
            return
        for origin in self.operand_origins(node):
            if origin[0] not in {"instance", "current", "field"}:
                continue
            if name := self.restrictions.truth_method(origin[1]):
                self.operator_call(node, node, origin, name)

    def membership(self, node: ast.Compare, container: ast.expr) -> None:
        for origin in self.operand_origins(container):
            if origin[0] not in {"instance", "current", "field"}:
                continue
            owner = origin[1]
            contains, absent = self.restrictions.special_lookup(owner, "__contains__")
            if contains:
                self.operator_call(node, container, origin, "__contains__")
                returned = self.restrictions.protocol_return_type(owner, "__contains__")
                if returned and (name := self.restrictions.truth_method(returned[1])):
                    self.operator_call(node, container, returned, name)
            elif absent:
                iterator, missing = self.restrictions.special_lookup(owner, "__iter__")
                if iterator:
                    elements = self.protocol_iteration(node, container, "__iter__", "__next__")
                elif missing:
                    self.operator_call(node, container, origin, "__getitem__")
                    returned = self.restrictions.protocol_return_type(owner, "__getitem__")
                    elements = frozenset({returned}) if returned else frozenset()
                else:
                    elements = frozenset()
                if elements:
                    _ = self.binary_dispatch(node, container, node.left, "__eq__", "__eq__",
                                             left_evidence=elements)

    @override
    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.generic_visit(node)
        names = _BINARY.get(type(node.op))
        if names:
            self._operator_results[id(node)] = self.binary_dispatch(
                node, node.left, node.right, names[0], names[1])

    @override
    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            self.truth(node.operand)
        elif name := _UNARY.get(type(node.op)):
            results: set[Binding] = set()
            for origin in self.operand_origins(node.operand):
                self.operator_call(node, node.operand, origin, name)
                if origin[0] in {"instance", "current", "field"} and (
                        returned := self.restrictions.protocol_return_type(origin[1], name)):
                    results.add(returned)
            self._operator_results[id(node)] = frozenset(results)

    @override
    def visit_Compare(self, node: ast.Compare) -> None:
        self.visit(node.left)
        left = node.left
        for index, (op, right) in enumerate(zip(node.ops, node.comparators)):
            self.visit(right)
            site = ast.copy_location(ast.Compare(left=left, ops=[op], comparators=[right]), node)
            if names := _COMPARE.get(type(op)):
                results = self.binary_dispatch(site, left, right, names[0], names[1])
                if index < len(node.ops) - 1:
                    self.truth_results(site, results)
                else:
                    self._operator_results[id(node)] = results
            elif isinstance(op, (ast.In, ast.NotIn)):
                self.membership(site, right)
            left = right

    def truth_results(self, node: ast.expr, origins: frozenset[Binding]) -> None:
        for origin in origins:
            if (origin[0] in {"instance", "current", "field"}
                    and (name := self.restrictions.truth_method(origin[1]))):
                self.operator_call(node, node, origin, name)

    @override
    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for value in node.values[:-1]:
            self.visit(value)
            self.truth(value)
        self.visit(node.values[-1])

    @override
    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        self.truth(node.test)
        self.visit(node.body)
        self.visit(node.orelse)

    @override
    def visit_Assert(self, node: ast.Assert) -> None:
        self.generic_visit(node)
        self.truth(node.test)

    def implicit_call(self, site: DispatchSite, *, intermediate: bool = False) -> None:
        for target, line, column in site.forbidden_targets(self.allowed, intermediate):
            self.diagnostics.append({
                "rule": "ENCAPSULATED_ONLY_CALL", "path": self.resolver.path,
                "line": line, "column": column,
                "symbol": target,
                "message": f"Implicit call to restricted method {target} requires the current instance, "
                           + "one direct stored instance field, or proven current-instance super."})

    def protocol_iteration(self, node: ast.expr, receiver: ast.expr, acquire: str,
                           advance: str) -> frozenset[Binding]:
        """Check acquisition and advancement without conferring creator permission.

        Args:
            node (ast.expr): Source operation for both diagnostics.
            receiver (ast.expr): Expression producing the iterator or awaitable.
            acquire (str): Acquisition hook name.
            advance (str): Advancement hook name.

        Returns:
            frozenset[Binding]: Proven types yielded by advancement.
        """
        site = self.dispatch_site(node, receiver, (acquire,))
        self.implicit_call(site)
        produced: set[Binding] = set()
        for kind, owner in site.origins:
            if kind not in {"instance", "current", "field"}:
                continue
            if returned := self.restrictions.protocol_return_type(owner, acquire):
                produced.add(returned)
                advancement = ((kind, owner)
                               if self.restrictions.protocol_returns_self(owner, acquire) else returned)
                self.operator_call(node, receiver, advancement, advance)
        return frozenset(returned for _, owner in produced
                          if (returned := self.restrictions.protocol_return_type(owner, advance)))

    def builtin_protocol(self, node: ast.Call) -> None:
        """Check supported builtins only after proving their lexical identity.

        Args:
            node (ast.Call): Potential builtin invocation.
        """
        name = next((candidate for candidate in PROTOCOL_BUILTINS
                     if self.builtin_call(node, candidate)), None)
        if (name is None or node.keywords or len(node.args) not in
                ({1, 2} if name in _BUILTIN_OPTIONAL_ARGUMENT else {1})):
            return
        first = node.args[0]
        if name in _BUILTIN_ITERATION:
            self.builtin_iteration(node, first, name)
            return
        for origin in self.origins(first):
            if origin[0] in {"instance", "current", "field"}:
                if name == "format":
                    self.builtin_format(node, first, origin)
                elif name in _BUILTIN_DIRECT:
                    self.operator_call(node, first, origin, _BUILTIN_DIRECT[name])
                else:
                    self.builtin_conversion(node, first, origin, name)

    def builtin_format(self, node: ast.Call, receiver: ast.expr, origin: Binding) -> None:
        """Follow the default empty-spec formatting fallback to string conversion.

        Args:
            node (ast.Call): Formatting call site.
            receiver (ast.expr): Formatted expression.
            origin (Binding): Possible receiver origin.
        """
        method, absent = self.restrictions.special_lookup(origin[1], "__format__")
        if method:
            self.operator_call(node, receiver, origin, "__format__")
        elif (absent and (len(node.args) == 1 or isinstance(node.args[1], ast.Constant)
                          and node.args[1].value == "")):
            self.builtin_conversion(node, receiver, origin, "str")

    def builtin_conversion(self, node: ast.Call, receiver: ast.expr,
                           origin: Binding, name: str) -> None:
        """Attempt a fallback only when earlier protocol slots are proven absent.

        Args:
            node (ast.Call): Conversion call site.
            receiver (ast.expr): Converted expression.
            origin (Binding): Possible receiver origin.
            name (str): Proven conversion builtin.
        """
        for hook in _BUILTIN_FALLBACK[name]:
            method, absent = self.restrictions.special_lookup(origin[1], hook)
            if method:
                self.operator_call(node, receiver, origin, hook)
                break
            if not absent:
                break

    def builtin_iteration(self, node: ast.Call, receiver: ast.expr, name: str) -> None:
        """Check acquisition and advancement with distinct receiver origins.

        Args:
            node (ast.Call): Helper call site.
            receiver (ast.expr): Iterable or iterator expression.
            name (str): Proven iterator helper.
        """
        if name in {"iter", "next"}:
            self.implicit_call(self.dispatch_site(
                node, receiver, ("__iter__" if name == "iter" else "__next__",)))
            return
        if name in {"list", "tuple"}:
            self.implicit_call(self.dispatch_site(node, receiver, ("__len__",)))
        elements = self.protocol_iteration(node, receiver, "__iter__", "__next__")
        if name in {"any", "all"}:
            self.truth_results(node, elements)

    def bind_protocol_result(self, target: ast.expr, results: frozenset[Binding]) -> None:
        """Bind an intermediate's result type without passing receiver permission.

        Args:
            target (ast.expr): Assignment target.
            results (frozenset[Binding]): Statically established possible types.
        """
        self._assigned_origin.clear()
        self._assigned_origin.update(results or frozenset({("unknown", "")}))
        self.assign(target, next(iter(results)) if len(results) == 1 else None)

    def comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
                      ) -> None:
        """Visit each iterable while containing target shadowing in the comprehension.

        Args:
            node (ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp): Comprehension.
        """
        self.visit(node.generators[0].iter)
        saved_valid = self.receiver_valid
        self.resolver.scopes.append({})
        self.provenance.append({})
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            results = self.protocol_iteration(generator.iter, generator.iter,
                                              "__aiter__" if generator.is_async else "__iter__",
                                              "__anext__" if generator.is_async else "__next__")
            self.bind_protocol_result(generator.target, results)
            for condition in generator.ifs:
                self.visit(condition)
                self.truth(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        _ = self.provenance.pop()
        _ = self.resolver.scopes.pop()
        self.receiver_valid = saved_valid

    @override
    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.comprehension(node)

    @override
    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.comprehension(node)

    @override
    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.comprehension(node)

    @override
    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.comprehension(node)

    @override
    def visit_Await(self, node: ast.Await) -> None:
        _ = self.protocol_iteration(node, node.value, "__await__", "__next__")
        self.visit(node.value)

    def attribute_dispatch(self, receiver: ast.expr, name: str, node: ast.expr,
                           operation: str) -> None:
        if operation == "read":
            names = ("__getattribute__",)
            # __getattr__ only participates when the normal lookup can miss, or a
            # custom __getattribute__ may raise AttributeError.
            origins = self.origins(receiver)
            if any(kind in {"instance", "current", "field"} and (
                    not self.restrictions.has_attribute(owner, name)
                    or self.restrictions.custom_attribute_lookup(owner))
                   for kind, owner in origins):
                names += ("__getattr__",)
        else:
            names = ("__setattr__",) if operation == "write" else ("__delattr__",)
        self.implicit_call(self.dispatch_site(node, receiver, names))

    @override
    def visit_Attribute(self, node: ast.Attribute) -> None:
        operation = ("delete" if isinstance(node.ctx, ast.Del) else
                     "write" if isinstance(node.ctx, ast.Store) else "read")
        self.property_access(node.value, node.attr, node, operation)
        self.attribute_dispatch(node.value, node.attr, node, operation)
        self.generic_visit(node)

    @override
    def visit_Subscript(self, node: ast.Subscript) -> None:
        name = ("__delitem__" if isinstance(node.ctx, ast.Del) else
                "__setitem__" if isinstance(node.ctx, ast.Store) else "__getitem__")
        self.implicit_call(self.dispatch_site(node, node.value, (name,)))
        self.generic_visit(node)

    @override
    def visit_Call(self, node: ast.Call) -> None:
        if self._ancestry_parents is not None:
            self.generic_visit(node)
            return
        truth_call = (self.resolver.builtin_call(node, "bool")
                      and len(node.args) == 1 and not node.keywords)
        for operation, count in (("getattr", 2), ("setattr", 3), ("delattr", 2)):
            if (self.resolver.builtin_call(node, operation) and len(node.args) == count
                    and not node.keywords and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                self.property_access(node.args[0], node.args[1].value, node,
                                      {"getattr": "read", "setattr": "write", "delattr": "delete"}[operation])
                self.attribute_dispatch(node.args[0], node.args[1].value, node,
                                        {"getattr": "read", "setattr": "write",
                                         "delattr": "delete"}[operation])
        for kind, owner in self.origins(node.func):
            if kind == "class":
                # Construction invokes hooks on a fresh object, not on the class
                # expression's privileged receiver.
                for name in ("__new__", "__init__"):
                    target = self.restrictions.special_target(owner, name)
                    if target:
                        self.implicit_call(DispatchSite(node, node.func,
                                                        frozenset({("fresh", owner)}), (target,)))
        self.implicit_call(self.dispatch_site(node, node.func, ("__call__",)))
        direct = isinstance(node.func, ast.Attribute) or (
            isinstance(node.func, ast.Call) and self.resolver.builtin_call(node.func, "getattr")
            and len(node.func.args) == 2 and not node.func.keywords
            and isinstance(node.func.args[1], ast.Constant)
            and isinstance(node.func.args[1].value, str))
        receiver = (node.func.value if isinstance(node.func, ast.Attribute) else
                    node.func.args[0] if direct and isinstance(node.func, ast.Call) else None)
        for target in sorted(self.targets(node.func)):
            if not direct or receiver is None or not self.allowed(receiver) or (
                    isinstance(node.func, ast.Attribute) and any(
                        kind == "class" for kind, _ in self.origins(receiver))):
                self.diagnostics.append({
                    "rule": "ENCAPSULATED_ONLY_CALL", "path": self.resolver.path,
                    "line": node.lineno, "column": node.col_offset + 1, "symbol": target,
                    "message": f"Call to restricted method {target} requires the current instance, " +
                               "one direct stored instance field, or proven current-instance super."})
        self.generic_visit(node)
        self.builtin_protocol(node)
        if truth_call:
            self.truth(node.args[0])

    @override
    def visit_Import(self, node: ast.Import) -> None:
        self.resolver.visit_Import(node)
        for alias in node.names:
            _ = self.provenance[-1].pop(alias.asname or alias.name.split(".")[0], None)

    @override
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.resolver.visit_ImportFrom(node)
        for alias in node.names:
            _ = self.provenance[-1].pop(alias.asname or alias.name, None)

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._ancestry_parents is not None:
            owner = f"{self.prefix}.{node.name}"
            bases: list[str | None] = []
            for base in node.bases:
                binding = self.value(base)
                resolved = binding[1] if binding and binding[0] == "class" else None
                if resolved is None:
                    fallback = self.restrictions.resolved_base(owner, base, self.resolver.path)
                    # A local class spelling is not proof if the binding was
                    # replaced or its decorator changed the runtime class.
                    if self.restrictions.contract_node(fallback) is None:
                        resolved = fallback
                bases.append(resolved)
            self._ancestry_parents[owner] = tuple(bases)
        for expression in (*node.decorator_list, *node.bases, *(item.value for item in node.keywords)):
            self.visit(expression)
        previous = self._context.copy()
        previous_prefix, previous_valid = self.prefix, self.receiver_valid
        previous_provenance = self.provenance.copy()
        # A nested class cannot borrow its enclosing method's identity.
        self.provenance[:] = [{name: origins for name, origins in scope.items()
                               if all(kind != "current" for kind, _ in origins)}
                              for scope in previous_provenance]
        qualified = f"{self.prefix}.{node.name}"
        self._context.update(current_class=qualified, receiver=None, method_owner=None)
        self.receiver_valid = False
        self.prefix = qualified
        self.resolver.scopes[-1][node.name] = (("class", qualified) if
                                             self.restrictions.known_class(qualified) else None)
        self.resolver.scopes.append({})
        self.provenance.append({})
        for statement in node.body:
            self.visit(statement)
        _ = self.resolver.scopes.pop()
        self.provenance[:] = previous_provenance
        self._context.update(previous)
        self.prefix, self.receiver_valid = previous_prefix, previous_valid
        self.provenance[-1][node.name] = (frozenset({("class", qualified)}) if
                                          self.restrictions.known_class(qualified) else
                                          frozenset({("unknown", "")}))

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        annotations = [arg.annotation for arg in (*node.args.posonlyargs, *node.args.args,
                                                   *node.args.kwonlyargs)]
        annotations.extend(arg.annotation for arg in (node.args.vararg, node.args.kwarg) if arg)
        for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults,
                           *annotations, node.returns):
            if expression is not None:
                self.visit(expression)
        owner = self._context["current_class"]
        previous = self._context.copy()
        previous_prefix, previous_valid = self.prefix, self.receiver_valid
        prior_provenance = self.provenance.copy()
        locals_: dict[str, Binding | None] = dict.fromkeys(local_names(node))
        args = node.args.posonlyargs + node.args.args
        for arg in (*args, *node.args.kwonlyargs):
            locals_[arg.arg] = self.resolver.annotation(arg.annotation, owner)
        instance = owner is not None and bool(args) and all(
            self.restrictions.decorator_is_instance(dec) for dec in node.decorator_list)
        self._context["receiver"] = args[0].arg if instance else None
        self.receiver_valid = instance
        if instance and owner is not None:
            locals_[args[0].arg] = ("instance", owner)
        class_namespace = self.resolver.scopes.pop() if owner else None
        self.resolver.scopes.append(locals_)
        self.provenance[:] = [*prior_provenance, {name: frozenset() for name in locals_}]
        if instance and owner is not None:
            self.provenance[-1][args[0].arg] = frozenset({("current", owner)})
        # Nested functions retain proven enclosing identity through lexical names.
        self._context["current_class"] = None
        self._context["method_owner"] = owner if instance else previous["method_owner"]
        self.prefix = f"{self.prefix}.{node.name}"
        for statement in node.body:
            self.visit(statement)
        _ = self.resolver.scopes.pop()
        self.provenance[:] = prior_provenance
        if class_namespace is not None:
            self.resolver.scopes.append(class_namespace)
        self._context.update(previous)
        self.prefix, self.receiver_valid = previous_prefix, previous_valid
        self.resolver.scopes[-1][node.name] = None
        self.provenance[-1][node.name] = frozenset({("unknown", "")})

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def assign(self, target: ast.expr, binding: Binding | None) -> None:
        if (isinstance(target, ast.Attribute) and self.value(target.value) == ("builtins", "builtins")):
            self._reassigned_builtins.add(target.attr)
        if isinstance(target, ast.Name) and target.id == self._context["receiver"]:
            self.receiver_valid = False
        self.resolver.assign(target, binding)
        if isinstance(target, ast.Name):
            self.provenance[-1][target.id] = frozenset(self._assigned_origin)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.assign(item, None)

    @override
    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        origins = self.origins(node.value)
        for target in node.targets:
            self.visit(target)
            self._assigned_origin.clear()
            self._assigned_origin.update(origins)
            self.assign(target, self.value(node.value))

    @override
    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        binding = self.resolver.annotation(node.annotation)
        inferred = self.value(node.value) if node.value is not None else None
        self._assigned_origin.clear()
        if node.value is not None:
            self._assigned_origin.update(self.origins(node.value))
        self.assign(node.target, binding if inferred is None or inferred == binding else None)

    @override
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Subscript):
            self.implicit_call(self.dispatch_site(node.target, node.target.value, ("__getitem__",)))
        elif isinstance(node.target, ast.Attribute):
            self.property_access(node.target.value, node.target.attr, node.target, "read")
            self.attribute_dispatch(node.target.value, node.target.attr, node.target, "read")
        names = _BINARY.get(type(node.op))
        if names:
            self.binary_dispatch(node, node.target, node.value, names[0], names[1], names[2])
        self.generic_visit(node)
        self._assigned_origin.clear()
        self.assign(node.target, None)

    @override
    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            self._assigned_origin.clear()
            self.assign(target, None)

    @override
    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._assigned_origin.clear()
        self._assigned_origin.update(self.origins(node.value))
        self.assign(node.target, self.value(node.value))

    @override
    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self.truth(node.test)
        self.branches([node.body, node.orelse])

    def branches(self, paths: list[list[ast.stmt]], invalid: set[str] | None = None) -> None:
        """Join possible paths without turning uncertainty into current-instance proof."""
        before = [scope.copy() for scope in self.resolver.scopes]
        origins_before = [scope.copy() for scope in self.provenance]
        valid = self.receiver_valid
        outcomes: list[tuple[list[dict[str, Binding | None]], list[dict[str, frozenset[Binding]]], bool]] = []
        for branch in paths:
            self.resolver.scopes = [scope.copy() for scope in before]
            self.provenance[:] = [scope.copy() for scope in origins_before]
            if invalid and branch is not paths[0]:
                for name in invalid:
                    self.resolver.scopes[-1][name] = None
                    self.provenance[-1][name] = frozenset({("unknown", "")})
            self.receiver_valid = valid and not (invalid and self._context["receiver"] in invalid)
            for statement in branch:
                self.visit(statement)
            outcomes.append(([scope.copy() for scope in self.resolver.scopes],
                             [scope.copy() for scope in self.provenance], self.receiver_valid))
        self.resolver.scopes = before
        self.provenance[:] = origins_before
        self.receiver_valid = all(outcome[2] for outcome in outcomes)
        for index, scope in enumerate(origins_before):
            names = {name for outcome in outcomes for name in outcome[1][index]}
            for name in names:
                scope[name] = frozenset().union(*(
                    outcome[1][index].get(name, frozenset({("unknown", "")}))
                    for outcome in outcomes))
        for index, scope in enumerate(before):
            names = {name for outcome in outcomes for name in outcome[0][index]}
            for name in names:
                bindings = [outcome[0][index].get(name) for outcome in outcomes]
                scope[name] = bindings[0] if all(item == bindings[0] for item in bindings) else None

    @override
    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        results = self.protocol_iteration(
            node.iter, node.iter, "__aiter__" if isinstance(node, ast.AsyncFor) else "__iter__",
            "__anext__" if isinstance(node, ast.AsyncFor) else "__next__")
        self.visit(node.iter)
        self.bind_protocol_result(node.target, results)
        self.conditional_body([*node.body, *node.orelse])

    @override
    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def conditional_body(self, statements: list[ast.stmt]) -> None:
        before = [scope.copy() for scope in self.resolver.scopes]
        origins_before = [scope.copy() for scope in self.provenance]
        valid = self.receiver_valid
        for statement in statements:
            self.visit(statement)
        after = self.resolver.scopes
        origins_after = [scope.copy() for scope in self.provenance]
        self.resolver.scopes = before
        self.provenance[:] = origins_before
        self.receiver_valid = valid and self.receiver_valid
        for scope, updated in zip(origins_before, origins_after):
            for name in scope.keys() | updated.keys():
                scope[name] = (scope.get(name, frozenset({("unknown", "")})) |
                               updated.get(name, frozenset({("unknown", "")})))
        for scope, updated in zip(before, after):
            for name in scope.keys() | updated.keys():
                if scope.get(name) != updated.get(name):
                    scope[name] = None

    @override
    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.truth(node.test)
        self.conditional_body([*node.body, *node.orelse])

    @override
    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        enter, exit_ = (("__aenter__", "__aexit__") if isinstance(node, ast.AsyncWith)
                        else ("__enter__", "__exit__"))
        for item in node.items:
            self.implicit_call(self.dispatch_site(item.context_expr, item.context_expr,
                                                  (enter, exit_)))
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                results = frozenset(returned for kind, owner in self.origins(item.context_expr)
                                    if kind in {"instance", "current", "field"}
                                    if (returned := self.restrictions.protocol_return_type(owner, enter)))
                self.bind_protocol_result(item.optional_vars, results)
        for statement in node.body:
            self.visit(statement)

    @override
    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    @override
    def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
        invalid = {name for statement in node.body for name in assigned_names(statement)}
        # Handler names and changes in the try suite cannot retain pre-try proof.
        handlers: list[list[ast.stmt]] = []
        for handler in node.handlers:
            if handler.type:
                self.visit(handler.type)
            if handler.name:
                invalid.add(handler.name)
            handlers.append(handler.body)
        self.branches([node.body + node.orelse, *handlers, []], invalid)
        for statement in node.finalbody:
            self.visit(statement)

    @override
    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        paths: list[list[ast.stmt]] = [[]]
        for case in node.cases:
            captured = {part.name for part in ast.walk(case.pattern)
                        if isinstance(part, (ast.MatchAs, ast.MatchStar)) and part.name}
            captured.update(part.rest for part in ast.walk(case.pattern)
                            if isinstance(part, ast.MatchMapping) and part.rest)
            for name in captured:
                self._assigned_origin.clear()
                self._assigned_origin.add(("unknown", ""))
                self.assign(ast.Name(id=name, ctx=ast.Store()), None)
            if case.guard:
                self.visit(case.guard)
                self.truth(case.guard)
            paths.append(case.body)
        self.branches(paths)

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Deferred lambda invocation and callable provenance are a later slice.
        pass
