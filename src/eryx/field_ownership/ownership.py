"""Index stored fields and their declaring classes across discovered source files."""
from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import override

from eryx._field_initialization import missing_fields, rebound_fields, written_fields
from eryx.field_operation_model.field_operation import field_operations
from eryx.field_type_analysis.type_evidence import PRIMITIVES, UNKNOWN, TypeEvidence, field_evidence
from eryx.field_ownership.bindings import Binding, PROTOCOL_BUILTINS, assigned_names, module_name
from eryx.field_access.accesses import Accesses
from eryx.field_ownership.class_stores import class_stores
from eryx.field_ownership.field_ownership import FieldOwnership
from eryx.marker_analysis.marker_index import MarkerIndex

__all__ = ["Binding", "Ownership", "PROTOCOL_BUILTINS", "assigned_names", "module_name"]

def receiver_fields(node: ast.ClassDef, tree: ast.Module) -> set[str]:
    # Only proven method receiver stores define fields; deferred scopes do not.
    fields: set[str] = set()
    for method in node.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(decorator, ast.Name) and decorator.id in {"classmethod", "staticmethod"}
                   for decorator in method.decorator_list):
                continue
            args = method.args.posonlyargs + method.args.args
            if args:
                for events in field_operations(tree, method, args[0].arg).values():
                    fields.update(event.name for event in events
                                  if event.kind in {"write", "setdefault", "augment"})
    return fields


def slot_locations(node: ast.expr) -> dict[str, ast.expr]:
    """Locate literal slot names at their string token, including dictionary keys."""
    special = {"__dict__", "__weakref__"}
    if isinstance(node, ast.Constant):
        return {node.value: node} if isinstance(node.value, str) and node.value not in special else {}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = node.elts
    elif isinstance(node, ast.Dict):
        items = node.keys
    else:
        return {}
    if not all(isinstance(item, ast.Constant) and isinstance(item.value, str)
               for item in items):
        return {}
    return {item.value: item for item in items if item.value not in special}


def dataclass_options(node: ast.ClassDef, names: set[str], modules: set[str]) -> tuple[bool, bool]:
    """Return whether the standard decorator generates an init and uses slots."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (isinstance(target, ast.Name) and target.id in names
                or isinstance(target, ast.Attribute) and target.attr == "dataclass"
                and isinstance(target.value, ast.Name) and target.value.id in modules):
            if isinstance(decorator, ast.Call) and any(item.arg is None for item in decorator.keywords):
                return False, False
            options = {item.arg: item.value for item in decorator.keywords} if isinstance(
                decorator, ast.Call) else {}
            init = options.get("init", ast.Constant(value=True))
            slots = options.get("slots", ast.Constant(value=False))
            return (isinstance(init, ast.Constant) and init.value is True,
                    isinstance(slots, ast.Constant) and slots.value is True)
    return False, False


def generated_field(item: ast.AnnAssign, field_names: set[str], modules: set[str],
                    slots: bool) -> bool:
    """Model the assignments emitted by dataclasses for a stored field."""
    value = item.value
    if not isinstance(value, ast.Call):
        return True
    target = value.func
    if not (isinstance(target, ast.Name) and target.id in field_names
            or isinstance(target, ast.Attribute) and target.attr == "field"
            and isinstance(target.value, ast.Name) and target.value.id in modules):
        return True
    if any(argument.arg is None for argument in value.keywords):
        return False
    options = {argument.arg: argument.value for argument in value.keywords}
    init = options.get("init", ast.Constant(value=True))
    if not isinstance(init, ast.Constant):
        return False
    if init.value is True:
        return True
    if init.value is not False:
        return False
    return "default_factory" in options or slots and "default" in options


def pseudo_field(annotation: ast.expr, names: set[str], modules: set[str]) -> bool:
    """InitVar and KW_ONLY are dataclass parameters/markers, not stored fields."""
    target = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return (isinstance(target, ast.Name) and target.id in names
            or isinstance(target, ast.Attribute) and target.attr in {"InitVar", "KW_ONLY"}
            and isinstance(target.value, ast.Name) and target.value.id in modules)


def dataclass_decorator(node: ast.expr, decorators: tuple[set[str], set[str]]) -> bool:
    functions, modules = decorators
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id in functions
            or isinstance(target, ast.Attribute) and target.attr == "dataclass"
            and isinstance(target.value, ast.Name) and target.value.id in modules)


def stable_class(node: ast.ClassDef, decorators: tuple[set[str], set[str]],
                 markers: MarkerIndex | None = None) -> bool:
    """An explicit dataclass decorator preserves the class's identity."""
    return all(markers is not None and markers.is_identity(dec)
               or dataclass_decorator(dec, decorators) for dec in node.decorator_list)


def classvar_annotation(node: ast.expr, names: set[str], modules: set[str]) -> bool:
    """Recognize explicit ClassVar annotations, including imported aliases."""
    target = node.value if isinstance(node, ast.Subscript) else node
    return (isinstance(target, ast.Name) and target.id in names
            or isinstance(target, ast.Attribute) and target.attr == "ClassVar"
            and isinstance(target.value, ast.Name) and target.value.id in modules)


class Ownership(FieldOwnership):
    """Index declarations across all discovered source files before checking uses."""

    def __init__(self, trees: dict[str, ast.Module]):
        self.trees = trees
        self.markers = MarkerIndex(trees)
        self.modules = {module_name(path): path for path in trees}
        self.fields: dict[str, set[str]] = {}
        self.class_fields: dict[str, set[str]] = {}
        self.instance_fields: dict[str, set[str]] = {}
        self.declarations: list[dict[str, object]] = []
        self.classes: dict[str, ast.ClassDef] = {}
        self.stable: set[str] = set()
        self.members: dict[str, set[str]] = {}
        self.bases: dict[str, tuple[str | None, ...]] = {}
        self.exports: dict[str, Binding] = {}
        self.type_evidence = TypeEvidence(trees)
        self.field_sources: dict[str, list[tuple[str, ast.ClassDef, dict[str, ast.expr], set[str]]]] = {}
        for path, tree in trees.items():
            self.index(tree.body, module_name(path), path, (set(), set()), (set(), set()),
                       (set(), set()))
        self.binding_diagnostics()
        # Resolve explicit exports to a fixed point so file order does not affect aliases.
        for _ in range(len(trees) + 1):
            previous = self.exports.copy()
            for path, tree in trees.items():
                visitor = Accesses(self, path)
                visitor.visit(tree)
                for name, binding in visitor.scopes[0].items():
                    if binding and binding[0] == "class":
                        self.exports[f"{visitor.module}.{name}"] = binding
            if self.exports == previous:
                break

    @override
    def source_trees(self) -> Iterator[tuple[str, ast.Module]]:
        return iter(self.trees.items())

    @override
    def source_fields(self) -> Iterator[tuple[str, list[tuple[str, ast.ClassDef,
                      dict[str, ast.expr], set[str]]]]]:
        return ((owner, sources.copy()) for owner, sources in self.field_sources.items())

    @override
    def marker_identity(self, node: ast.expr) -> bool:
        return self.markers.is_identity(node)

    @override
    def marker_symbol(self, node: ast.expr) -> str | None:
        return self.markers.symbol(node)

    @override
    def descriptors(self, node: ast.ClassDef) -> dict[str, list[tuple[ast.stmt, str, str]]]:
        return self.markers.property_declarations(node)

    @override
    def field_names(self, owner: str) -> set[str]:
        return self.fields[owner].copy()

    @override
    def class_field_names(self, owner: str) -> set[str]:
        return self.class_fields[owner].copy()

    @override
    def is_instance_field(self, owner: str, name: str) -> bool:
        return name in self.instance_fields[owner]

    @override
    def type_proof(self, node: ast.ClassDef, field: str, path: str
                   ) -> tuple[list[str], list[str]]:
        return field_evidence(node, {field}, module_name(path),
                              self.type_evidence, self.trees[path])[field]

    @override
    def expression_type(self, node: ast.expr, path: str) -> str:
        return self.type_evidence.expression(node, module_name(path))

    def declaration_diagnostics(self) -> list[dict[str, object]]:
        """Return prohibited class declarations found in discovered files."""
        return self.declarations

    def binding_diagnostics(self) -> None:
        """Require exact types, forbid non-primitive rebinding and bound-field deletion."""
        start = len(self.declarations)
        for owner, sources in self.field_sources.items():
            for path, node, locations, generated in sources:
                for field, location in locations.items():
                    if path.endswith(".pyi") and any(
                            source_path.endswith(".py") and field in source_locations
                            for source_path, _, source_locations, _ in sources):
                        continue
                    annotations: list[str] = []
                    writes: list[str] = []
                    source_annotations: list[str] = []
                    for other_path, other, other_locations, _ in sources:
                        if field not in other_locations:
                            continue
                        evidence = field_evidence(other, {field}, module_name(other_path),
                                                  self.type_evidence, self.trees[other_path])[field]
                        annotations.extend(evidence[0])
                        if other_path.endswith(".py"):
                            source_annotations.extend(evidence[0])
                            writes.extend(evidence[1])
                    proof = (annotations if source_annotations else annotations + writes
                             if annotations else writes)
                    kind = next(iter(proof), UNKNOWN)
                    rebindings, deletions = rebound_fields(node, {field}, generated,
                                                             self.trees[path])
                    for store in deletions.get(field, []):
                        self.declarations.append({
                            "rule": "DELETED_INSTANCE_FIELD", "path": path,
                            "line": store.lineno, "column": store.col_offset + 1,
                            "symbol": f"{owner}.{field}",
                            "message": f"Deletion of initialized instance field {owner}.{field} is prohibited."})
                    if kind == UNKNOWN or any(item != kind for item in proof):
                        self.declarations.append({
                            "rule": "UNKNOWN_FIELD_TYPE", "path": path,
                            "line": location.lineno, "column": location.col_offset + 1,
                            "symbol": f"{owner}.{field}",
                            "message": f"Stored instance field {owner}.{field} has no single resolved type."})
                        continue
                    if kind in {f"builtins.{name}" for name in PRIMITIVES} or path.endswith(".pyi"):
                        continue
                    for store in rebindings.get(field, []):
                        self.declarations.append({
                            "rule": "NON_PRIMITIVE_FIELD_REBINDING", "path": path,
                            "line": store.lineno, "column": store.col_offset + 1,
                            "symbol": f"{owner}.{field}",
                            "message": f"Reassignment of non-primitive field {owner}.{field} is prohibited."})
        self.declarations[start:] = sorted(
            self.declarations[start:],
            key=lambda item: (str(item["path"]), int(item["line"]), int(item["column"])))

    def index(self, body: list[ast.stmt], prefix: str, path: str,
              decorators: tuple[set[str], set[str]],
              enums: tuple[set[str], set[str]],
              classvars: tuple[set[str], set[str]],
              field_names: set[str] | None = None,
              pseudo_names: set[str] | None = None) -> None:
        functions, modules = (set(decorators[0]), set(decorators[1]))
        enum_names, enum_modules = (set(enums[0]), set(enums[1]))
        classvar_names, classvar_modules = (set(classvars[0]), set(classvars[1]))
        field_names = set(field_names or ())
        pseudo_names = set(pseudo_names or ())
        descriptor_names: set[str] = set()
        descriptor_modules: set[str] = set()
        for node in body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    functions.discard(bound)
                    modules.discard(bound)
                    enum_names.discard(bound)
                    classvar_names.discard(bound)
                    descriptor_names.discard(bound)
                    field_names.discard(bound)
                    pseudo_names.discard(bound)
                    if node.level == 0 and node.module == "dataclasses" and alias.name == "dataclass":
                        functions.add(bound)
                    if node.level == 0 and node.module == "dataclasses" and alias.name == "field":
                        field_names.add(bound)
                    if node.level == 0 and node.module == "dataclasses" and alias.name in {"InitVar", "KW_ONLY"}:
                        pseudo_names.add(bound)
                    if node.level == 0 and node.module == "enum" and alias.name in {
                            "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}:
                        enum_names.add(bound)
                    if node.level == 0 and node.module in {"typing", "typing_extensions"} and alias.name == "ClassVar":
                        classvar_names.add(bound)
                    if node.level == 0 and node.module == "functools" and alias.name == "cached_property":
                        descriptor_names.add(bound)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    functions.discard(bound)
                    modules.discard(bound)
                    enum_modules.discard(bound)
                    classvar_modules.discard(bound)
                    descriptor_modules.discard(bound)
                    field_names.discard(bound)
                    pseudo_names.discard(bound)
                    if alias.name == "dataclasses":
                        modules.add(bound)
                    if alias.name == "enum":
                        enum_modules.add(bound)
                    if alias.name in {"typing", "typing_extensions"}:
                        classvar_modules.add(bound)
                    if alias.name == "functools":
                        descriptor_modules.add(bound)
            if isinstance(node, ast.ClassDef):
                name = f"{prefix}.{node.name}"
                self.classes[name] = node
                is_dataclass = (any(dataclass_decorator(dec, (functions, modules))
                                    for dec in node.decorator_list)
                                and stable_class(node, (functions, modules), self.markers))
                generates_init, dataclass_slots = dataclass_options(node, functions, modules)
                generates_init = is_dataclass and generates_init and not any(
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__"
                    for item in node.body)
                if stable_class(node, (functions, modules), self.markers):
                    self.stable.add(name)
                is_enum = any(
                    isinstance(base, ast.Name) and base.id in enum_names
                    or isinstance(base, ast.Attribute) and base.attr in {
                        "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
                    and isinstance(base.value, ast.Name) and base.value.id in enum_modules
                    for base in node.bases)
                def known_decorator(dec: ast.expr) -> bool:
                    if self.markers.is_identity(dec):
                        return True
                    if isinstance(dec, ast.Call):
                        dec = dec.func
                    if isinstance(dec, ast.Name):
                        return dec.id in {"property", "classmethod", "staticmethod", "override", "abstractmethod"} | descriptor_names
                    return (isinstance(dec, ast.Attribute) and
                            (dec.attr in {"setter", "deleter", "getter"}
                             and isinstance(dec.value, ast.Name)
                             or dec.attr == "cached_property" and isinstance(dec.value, ast.Name)
                             and dec.value.id in descriptor_modules))

                declarations = {item.name: item for item in node.body
                                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
                methods = {method for method, item in declarations.items()
                           if all(known_decorator(dec) for dec in item.decorator_list)}
                decorated_fields = declarations.keys() - methods
                declared = {target.id for item in node.body for target in (
                    item.targets if isinstance(item, ast.Assign) else
                    [item.target] if isinstance(item, ast.AnnAssign) else [])
                    if isinstance(target, ast.Name)}
                slot_declarations: dict[str, ast.expr] = {}
                for item in node.body:
                    if isinstance(item, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id == "__slots__"
                            for target in item.targets):
                        slot_declarations.update(slot_locations(item.value))
                    elif (isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                          and item.target.id == "__slots__" and item.value):
                        slot_declarations.update(slot_locations(item.value))
                slots = set(slot_declarations)
                properties = set(self.markers.property_declarations(node))
                instance = receiver_fields(node, self.trees[path]) | slots | {
                    item.target.id for item in node.body if isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name) and item.value is None
                    and not classvar_annotation(item.annotation, classvar_names, classvar_modules)
                    and not (is_dataclass and pseudo_field(item.annotation, pseudo_names, modules))}
                dataclass_fields = {item.target.id for item in node.body
                                    if is_dataclass and isinstance(item, ast.AnnAssign)
                                    and isinstance(item.target, ast.Name)
                                    and not classvar_annotation(
                                        item.annotation, classvar_names, classvar_modules)
                                    and not pseudo_field(item.annotation, pseudo_names, modules)}
                instance.update(dataclass_fields)
                generated = {item.target.id for item in node.body
                             if generates_init and isinstance(item, ast.AnnAssign)
                             and isinstance(item.target, ast.Name)
                             and item.target.id in dataclass_fields
                             and generated_field(item, field_names, modules, dataclass_slots)}
                pseudo_fields = {item.target.id for item in node.body
                                 if is_dataclass and isinstance(item, ast.AnnAssign)
                                 and isinstance(item.target, ast.Name)
                                 and pseudo_field(item.annotation, pseudo_names, modules)}
                field_declarations = {item.target.id: item.target for item in node.body
                                      if isinstance(item, ast.AnnAssign)
                                      and isinstance(item.target, ast.Name)
                                      and item.target.id in instance}
                field_declarations.update(slot_declarations)
                class_fields = ({target.id for item in node.body for target in (
                    item.targets if isinstance(item, ast.Assign) else
                    [item.target] if isinstance(item, ast.AnnAssign) and item.value is not None else [])
                    if isinstance(target, ast.Name)} | decorated_fields) - {
                        "__slots__"} - methods - properties - dataclass_fields - pseudo_fields
                if is_enum:
                    class_fields = {field for field in class_fields if field.startswith("_")}
                self.members[name] = declared | methods | {
                    item.name for item in node.body if isinstance(item, ast.ClassDef)}
                self.class_fields[name] = class_fields
                self.instance_fields[name] = instance - methods - properties - {"__slots__"}
                self.fields[name] = (class_fields | instance) - methods - properties - {"__slots__"}
                locations = {**written_fields(node, self.trees[path]), **field_declarations}
                locations = {field: location for field, location in locations.items()
                             if field in self.instance_fields[name]}
                self.field_sources.setdefault(name, []).append((path, node, locations, generated))
                if path.endswith(".py"):
                    for field, location in missing_fields(node, field_declarations, generated,
                                                          self.trees[path]).items():
                        if field not in self.instance_fields[name]:
                            continue
                        self.declarations.append({
                            "rule": "MISSING_FIELD_INITIALIZATION", "path": path,
                            "line": location.lineno, "column": location.col_offset + 1,
                            "symbol": f"{name}.{field}",
                            "message": f"Stored instance field {name}.{field} is not initialized "
                                       "in its owning constructor."})
                for field, target, annotated, value in class_stores(node):
                    is_property = (field in properties and isinstance(value, ast.Call)
                                   and any(item in self.markers.descriptor_assignments
                                           for item in node.body if isinstance(item, (ast.Assign, ast.AnnAssign))
                                           and item.value is value))
                    if (field == "__slots__" or is_property
                            or is_dataclass and annotated and field in dataclass_fields
                            or is_dataclass and annotated and field in pseudo_fields
                            or is_enum and not field.startswith("_")):
                        continue
                    self.declarations.append({
                        "rule": "NO_CLASS_STATE", "path": path,
                        "line": target.lineno, "column": target.col_offset + 1,
                        "symbol": f"{name}.{field}",
                        "message": f"Stored class value {name}.{field} is prohibited."})
                for field in sorted(decorated_fields - properties, key=lambda item: declarations[item].lineno):
                    method = declarations[field]
                    self.declarations.append({
                        "rule": "NO_CLASS_STATE", "path": path,
                        "line": method.lineno, "column": method.col_offset + 1,
                        "symbol": f"{name}.{field}",
                        "message": f"Stored class value {name}.{field} is prohibited."})
                self.index(node.body, name, path, (functions, modules), (enum_names, enum_modules),
                           (classvar_names, classvar_modules), field_names, pseudo_names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.index(node.body, f"{prefix}.{node.name}", path,
                           (functions, modules), (enum_names, enum_modules),
                           (classvar_names, classvar_modules), field_names, pseudo_names)
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for bound in assigned_names(node):
                    functions.discard(bound)
                    modules.discard(bound)
                    enum_names.discard(bound)
                    enum_modules.discard(bound)
                    classvar_names.discard(bound)
                    classvar_modules.discard(bound)
                    descriptor_names.discard(bound)
                    descriptor_modules.discard(bound)
                    field_names.discard(bound)
                    pseudo_names.discard(bound)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.discard(node.name)
                modules.discard(node.name)
                enum_names.discard(node.name)
                enum_modules.discard(node.name)
                classvar_names.discard(node.name)
                classvar_modules.discard(node.name)
                descriptor_names.discard(node.name)
                descriptor_modules.discard(node.name)
                field_names.discard(node.name)
                pseudo_names.discard(node.name)

    def field_owner(self, qualified: str, field: str,
                    seen: frozenset[str] | None = None) -> str | None:
        """Return a unique proven declaring class; never guess an unknown base's fields."""
        if seen is None:
            seen = frozenset()
        if qualified in seen or qualified not in self.stable:
            return None
        if field in self.fields[qualified]:
            return qualified
        if field in self.members[qualified]:
            return None
        bases = self.bases.get(qualified, ())
        if any(base is None for base in bases):
            return None
        owners = {owner for base in bases if base is not None
                  if (owner := self.field_owner(base, field, seen | {qualified})) is not None}
        return next(iter(owners)) if len(owners) == 1 else None

    def class_field_owner(self, qualified: str, field: str,
                          seen: frozenset[str] | None = None) -> str | None:
        if seen is None:
            seen = frozenset()
        if qualified in seen or qualified not in self.stable:
            return None
        if field in self.class_fields[qualified]:
            return qualified
        if field in self.members[qualified] and field not in self.instance_fields[qualified]:
            return None
        bases = self.bases.get(qualified, ())
        if any(base is None for base in bases):
            return None
        owners = {owner for base in bases if base is not None
                  if (owner := self.class_field_owner(base, field, seen | {qualified})) is not None}
        return next(iter(owners)) if len(owners) == 1 else None

    def first_write_owner(self, qualified: str, field: str) -> str | None:
        owner = self.field_owner(qualified, field)
        if owner:
            return owner

        def vacant(candidate: str, seen: frozenset[str]) -> bool:
            if (candidate in seen or candidate not in self.stable
                    or field in self.fields[candidate] or field in self.members[candidate]):
                return False
            return all(base is not None and vacant(base, seen | {candidate})
                       for base in self.bases.get(candidate, ()))

        return qualified if vacant(qualified, frozenset()) else None

    def member(self, owner: Binding | None, name: str) -> Binding | None:
        if owner is None:
            return None
        kind, qualified = owner
        if kind == "builtins":
            return (("builtin", name) if name in PROTOCOL_BUILTINS |
                    {"getattr", "setattr", "delattr", "vars", "super", "bool"} else None)
        if kind == "module":
            candidate = f"{qualified}.{name}"
            if candidate in self.modules:
                return ("module", candidate)
            if candidate in self.stable:
                return ("class", candidate)
            return self.exports.get(candidate)
        if kind == "class" and f"{qualified}.{name}" in self.stable:
            return ("class", f"{qualified}.{name}")
        return None
