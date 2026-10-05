"""Syntactic field declarations, dataclass assignments and stable owners."""
from __future__ import annotations

import ast

from quard.field_operation_model.operation_utils import field_operations
from quard.marker_analysis.marker_index import MarkerIndex


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
