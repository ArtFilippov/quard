"""Index restriction declarations and resolve effective dispatch targets."""
from __future__ import annotations

import ast
from typing import override

from eryx.contract_analysis.contracts import Contracts
from eryx.field_ownership.bindings import Binding, assigned_names, module_name
from eryx.field_ownership.ownership import Ownership
from eryx.field_type_analysis.type_evidence import UNKNOWN, field_evidence
from eryx.marker_analysis.marker_index import FUNCTIONS, local_names
from eryx.restricted_targets.restricted_targets import RestrictedTargets


class Restrictions(RestrictedTargets):
    """Retain every method declaration rather than collapsing source/stub/overloads."""

    def __init__(self, ownership: Ownership):
        super().__init__()
        self.ownership = ownership
        self.declarations: list[dict[str, object]] = []
        self.class_declarations: list[dict[str, object]] = []
        self.effective_targets: dict[str, str] = {}
        self.methods: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.own_methods: dict[str, set[str]] = {}
        self.properties: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.own_properties: dict[str, set[str]] = {}
        self.class_marked: set[str] = set()
        self.contracts = Contracts(ownership.trees)
        self.parents: dict[str, tuple[str | None, ...]] = {}
        self.field_types: dict[tuple[str, str], str] = {}
        primary = {owner: next((path for path, _, _, _ in sources if path.endswith(".py")),
                               sources[0][0]) for owner, sources in ownership.field_sources.items()}
        # Resolve base expressions in their declaration-time lexical scope, after
        # the cross-module export index reaches its fixed point.
        from eryx._restricted_calls import Ancestry
        for path, tree in ownership.trees.items():
            ancestry = Ancestry(self, path)
            ancestry.visit(tree)
            for owner, bases in ancestry.parents.items():
                if owner not in self.parents or path.endswith(".py"):
                    self.parents[owner] = bases
        for owner, sources in ownership.field_sources.items():
            for path, node, _, _ in sources:
                class_marker = any(ownership.markers.is_identity(dec) for dec in node.decorator_list)
                self.class_declarations.append({"owner": owner, "path": path,
                                                "line": node.lineno, "column": node.col_offset + 1,
                                                "marked": class_marker})
                if class_marker:
                    self.class_marked.add(owner)
                descriptors = ownership.markers.property_declarations(node)
                functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
                referenced_by: dict[ast.stmt, list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]] = {}
                for statement in node.body:
                    if isinstance(statement, (ast.Assign, ast.AnnAssign)) and isinstance(statement.value, ast.Call):
                        arguments = [(role, arg) for role, arg in zip(
                            ("getter", "setter", "deleter"), statement.value.args)]
                        arguments.extend(({"fget": "getter", "fset": "setter", "fdel": "deleter",
                                           "func": "getter"}.get(keyword.arg or "", ""), keyword.value)
                                         for keyword in statement.value.keywords)
                        referenced_by[statement] = [(role, functions[arg.id]) for role, arg in arguments
                                                     if role and isinstance(arg, ast.Name) and arg.id in functions]
                    for bound in assigned_names(statement) if not isinstance(statement, FUNCTIONS) else ():
                        _ = functions.pop(bound, None)
                    if isinstance(statement, FUNCTIONS):
                        functions[statement.name] = statement
                for name, entries in descriptors.items():
                    for member, role, kind in entries:
                        marked = (any(ownership.markers.is_identity(dec) for dec in member.decorator_list)
                                  if isinstance(member, FUNCTIONS) else False)
                        evidence: dict[str, object] = {"owner": owner, "member": name, "path": path,
                            "line": member.lineno, "column": member.col_offset + 1,
                            "marked": marked, "kind": kind, "accessor": role}
                        self.declarations.append(evidence)
                        self.properties.setdefault((owner, name), []).append(evidence)
                        for accessor, function in referenced_by.get(member, ()):
                            accessor_evidence: dict[str, object] = {"owner": owner, "member": name, "path": path,
                                "line": function.lineno, "column": function.col_offset + 1,
                                "marked": any(ownership.markers.is_identity(dec)
                                              for dec in function.decorator_list),
                                "kind": kind, "accessor": accessor}
                            self.declarations.append(accessor_evidence)
                            self.properties[owner, name].append(accessor_evidence)
                        if path == primary[owner]:
                            self.own_properties.setdefault(owner, set()).add(name)
                for member in node.body:
                    if isinstance(member, FUNCTIONS):
                        decorators = member.decorator_list
                        symbols = {ownership.markers.symbol(dec) for dec in decorators
                                   if not ownership.markers.is_identity(dec)}
                        if not symbols <= {"abc.abstractmethod", "typing.override",
                                            "typing.overload", "typing_extensions.override",
                                            "typing_extensions.overload"}:
                            continue
                        if member.name in descriptors:
                            continue
                        evidence: dict[str, object] = {"owner": owner, "member": member.name, "path": path,
                                    "line": member.lineno, "column": member.col_offset + 1,
                                    "marked": any(ownership.markers.is_identity(dec) for dec in decorators)}
                        self.declarations.append(evidence)
                        self.methods.setdefault((owner, member.name), []).append(evidence)
                        if path == primary[owner]:
                            self.own_methods.setdefault(owner, set()).add(member.name)
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

    @override
    def instance_type(self, qualified: str | None) -> Binding | None:
        if qualified in self.ownership.stable:
            return ("instance", qualified)
        exported = self.ownership.exports.get(qualified or "")
        return ("instance", exported[1]) if exported and exported[0] == "class" else None

    @override
    def target(self, owner: str, name: str) -> str | None:
        return self._target(owner, name, property_access=False)

    @override
    def property_target(self, owner: str, name: str) -> str | None:
        return self._target(owner, name, property_access=True)

    @override
    def special_target(self, owner: str, name: str) -> str | None:
        """Resolve type-level protocol lookup, never an instance field or property."""
        return self.target(owner, name) if self.special_method(owner, name) else None

    @override
    def special_method(self, owner: str, name: str) -> str | None:
        """Find the dispatched method even when it is not restricted."""
        method, _ = self.special_lookup(owner, name)
        return method

    @override
    def special_lookup(self, owner: str, name: str) -> tuple[str | None, bool]:
        """Return the method and whether absence is proved across the MRO."""
        order = self.mro(owner)
        if order is None:
            return None, False
        for base in order:
            if base not in self.ownership.stable:
                return None, False
            if (name in self.own_properties.get(base, ())
                    or self.ownership.class_field_owner(base, name) == base):
                return None, False
            if name in self.own_methods.get(base, ()):
                return f"{base}.{name}", False
        return None, True

    @override
    def truth_method(self, owner: str) -> str | None:
        method, absent = self.special_lookup(owner, "__bool__")
        return "__bool__" if method else "__len__" if absent else None

    @override
    def comparison_method(self, owner: str, name: str) -> str:
        """The default inequality slot delegates to equality when unoverridden."""
        if name == "__ne__" and self.special_lookup(owner, name)[1]:
            return "__eq__"
        return name

    @override
    def method_outcome(self, owner: str, name: str) -> str:
        """Only a direct, unambiguous return proves an operator's fallback fate."""
        symbol = self.special_method(owner, name)
        if symbol is None:
            return "missing"
        declaring = symbol.rpartition(".")[0]
        node = self.ownership.classes.get(declaring)
        if node is None:
            return "possible"
        methods = [item for item in node.body if isinstance(item, FUNCTIONS) and item.name == name]
        if len(methods) != 1 or len(methods[0].body) != 1:
            return "possible"
        statement = methods[0].body[0]
        if not isinstance(statement, ast.Return) or statement.value is None:
            return "possible"
        value = statement.value
        if (isinstance(value, ast.Name) and value.id == "NotImplemented"
                and "NotImplemented" not in local_names(methods[0])
                and not any("NotImplemented" in assigned_names(item)
                            or isinstance(item, (ast.FunctionDef, ast.ClassDef))
                            and item.name == "NotImplemented"
                            or isinstance(item, (ast.Import, ast.ImportFrom))
                            and any((alias.asname or alias.name.split(".")[0]) == "NotImplemented"
                                    for alias in item.names)
                            for tree in self.ownership.trees.values() for item in tree.body)):
            return "fallback"
        if isinstance(value, ast.Constant) or (isinstance(value, ast.Name)
                and bool(methods[0].args.posonlyargs + methods[0].args.args)
                and value.id == (methods[0].args.posonlyargs + methods[0].args.args)[0].arg):
            return "value"
        return "possible"

    @override
    def has_attribute(self, owner: str, name: str) -> bool:
        """Prove ordinary default instance lookup finds a declared member."""
        order = self.mro(owner)
        return bool(order and any(
            name in self.own_methods.get(base, ()) or name in self.own_properties.get(base, ())
            or self.ownership.field_owner(owner, name) is not None
            or self.ownership.class_field_owner(base, name) == base
            for base in order if base in self.ownership.stable))

    @override
    def custom_attribute_lookup(self, owner: str) -> bool:
        order = self.mro(owner) or []
        return any("__getattribute__" in self.own_methods.get(base, ())
                   for base in order if base in self.ownership.stable)

    def _target(self, owner: str, name: str, property_access: bool) -> str | None:
        if owner not in self.ownership.stable:
            return None
        order = self.mro(owner)
        if order is None:
            return None
        def declared(candidate: str) -> bool:
            return (name in self.own_methods.get(candidate, ())
                    or name in self.own_properties.get(candidate, ()))
        declaring = next((candidate for candidate in order if declared(candidate)), None)
        if (declaring is None or declaring not in self.ownership.stable
                or property_access != (name in self.own_properties.get(declaring, ()))):
            return None
        # An earlier opaque base may supply the runtime method instead.
        if any(candidate not in self.ownership.stable for candidate in order[:order.index(declaring)]):
            return None
        declarations = self.properties if property_access else self.methods
        direct = any(item["marked"] for item in declarations.get((declaring, name), ()))
        origin = f"{declaring}.{name}" if direct else None
        if not origin:
            for candidate in order:
                if candidate in self.class_marked and (property_access or not (
                        name.startswith("__") and name.endswith("__"))):
                    ancestor_order = self.mro(candidate)
                    if ancestor_order and next((base for base in ancestor_order
                                                if declared(base)), None) == declaring:
                        origin = candidate
                        break
        # Explicitly inherited contracts constrain a new implementation even
        # when it is not the method that wins dispatch on the contract itself.
        if not origin:
            for candidate in order[1:]:
                if candidate in self.contracts.nodes and self.contracts.declaration(candidate):
                    requirement = self.contract_target(candidate, name, property_access)
                    if requirement:
                        origin = requirement
                        break
        if origin:
            symbol = f"{declaring}.{name}"
            self.effective_targets[symbol] = origin
            return symbol
        return None

    @override
    def contract_target(self, owner: str, name: str, property_access: bool = False) -> str | None:
        order = self.mro(owner)
        if order is None:
            return None
        def declared(base: str) -> bool:
            return (name in self.own_properties.get(base, ()) if property_access
                    else name in self.own_methods.get(base, ()))
        declaring = next((base for base in order if declared(base)), None)
        for candidate in order:
            declarations = self.properties if property_access else self.methods
            if any(item["marked"] for item in declarations.get((candidate, name), ())):
                return f"{candidate}.{name}"
            if candidate in self.class_marked and (property_access or not (
                    name.startswith("__") and name.endswith("__"))):
                lineage = self.mro(candidate)
                if lineage and next((base for base in lineage
                                     if declared(base)), None) == declaring:
                    return candidate
        return None

    @override
    def mro(self, owner: str, visiting: frozenset[str] | None = None) -> list[str] | None:
        """Compute a conservative C3 linearization, retaining opaque base slots."""
        if visiting is None:
            visiting = frozenset()
        if owner in visiting:
            return None
        bases = self.parents.get(owner, ())
        resolved = [base if base is not None else f"<opaque:{owner}:{index}>"
                    for index, base in enumerate(bases)]
        sequences: list[list[str]] = []
        for base in resolved:
            lineage = self.mro(base, visiting | {owner}) if base in self.parents else [base]
            if lineage is None:
                return None
            sequences.append(list(lineage))
        sequences.append(resolved.copy())
        result = [owner]
        while any(sequences):
            candidate = next((seq[0] for seq in sequences if seq and all(
                seq[0] not in other[1:] for other in sequences)), None)
            if candidate is None:
                return None
            result.append(candidate)
            for seq in sequences:
                if seq and seq[0] == candidate:
                    _ = seq.pop(0)
        return result

    @override
    def field_type(self, owner: str, name: str) -> Binding | None:
        declaring = self.ownership.field_owner(owner, name)
        if declaring is None:
            return None
        qualified = self.field_types.get((declaring, name))
        return self.instance_type(qualified)

    @override
    def return_type(self, owner: str, name: str, property_only: bool = False) -> Binding | None:
        order = self.mro(owner) or [owner]
        declarations = self.own_properties if property_only else self.own_methods
        owner = next((base for base in order if name in declarations.get(base, ())), owner)
        node = self.ownership.classes.get(owner)
        if node is None:
            return None
        descriptors = self.ownership.markers.property_declarations(node).get(name, [])
        getters = {item for item, role, _ in descriptors if role == "getter"}
        for item, role, _ in descriptors:
            if role == "constructor" and isinstance(item, (ast.Assign, ast.AnnAssign)) \
                    and isinstance(item.value, ast.Call):
                args = [*item.value.args, *(kw.value for kw in item.value.keywords if kw.arg in {"fget", "func"})]
                getters.update(function for function in node.body if isinstance(function, FUNCTIONS)
                               for arg in args[:1] if isinstance(arg, ast.Name) and arg.id == function.name)
        returns: set[str] = set()
        for item in node.body:
            if not isinstance(item, FUNCTIONS) or item.name != name or (property_only and item not in getters):
                continue
            if item.returns is not None:
                returns.add(self.ownership.type_evidence.annotation(item.returns,
                                                                    owner.rpartition(".")[0]))
            elif (isinstance(item, ast.FunctionDef) and len(item.body) == 1
                  and isinstance(item.body[0], ast.Return) and item.body[0].value is not None):
                returns.add(self.ownership.type_evidence.expression(
                    item.body[0].value, owner.rpartition(".")[0], local_names(item)))
        if property_only:
            returns.update(self.ownership.type_evidence.annotation(item.returns, owner.rpartition(".")[0])
                           for item in getters if isinstance(item, FUNCTIONS) and item.returns is not None)
        qualified = next(iter(returns)) if len(returns) == 1 else None
        return self.instance_type(qualified)

    @override
    def protocol_return_type(self, owner: str, name: str) -> Binding | None:
        """Infer a special method's result only after proving type-level lookup.

        Args:
            owner (str): Receiver class.
            name (str): Protocol method name.

        Returns:
            Binding | None: Resolved result type, when available.
        """
        order = self.mro(owner)
        if order is None:
            return None
        for base in order:
            if base not in self.ownership.stable:
                return None
            if (name in self.own_properties.get(base, ())
                    or self.ownership.class_field_owner(base, name) == base):
                return None
            if name in self.own_methods.get(base, ()):
                return (("instance", owner) if self.protocol_returns_self(owner, name)
                        else self.return_type(owner, name))
        return None

    @override
    def protocol_returns_self(self, owner: str, name: str) -> bool:
        """Prove the dispatched source hook returns its unchanged first receiver.

        Args:
            owner (str): Receiver class.
            name (str): Protocol method name.

        Returns:
            bool: Whether the hook directly returns its receiver.
        """
        order = self.mro(owner)
        if order is None:
            return False
        for base in order:
            if (base not in self.ownership.stable or name in self.own_properties.get(base, ())
                    or self.ownership.class_field_owner(base, name) == base):
                return False
            if name in self.own_methods.get(base, ()):
                node = self.ownership.classes.get(base)
                if node is None:
                    return False
                methods = [item for item in node.body if isinstance(item, ast.FunctionDef)
                           and item.name == name]
                return len(methods) == 1 and len(methods[0].body) == 1 and (
                    isinstance(methods[0].body[0], ast.Return) and
                    isinstance(methods[0].body[0].value, ast.Name) and
                    bool(methods[0].args.posonlyargs + methods[0].args.args) and
                    methods[0].body[0].value.id == (
                        methods[0].args.posonlyargs + methods[0].args.args)[0].arg)
        return False
