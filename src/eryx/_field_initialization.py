"""Prove constructor field bindings on every path that creates a usable instance."""
from __future__ import annotations

import ast
from eryx.field_operation_model.field_operation import FieldOperation, field_operations

# A count saturates at two: branches join as alternatives, whereas loop iterations
# can produce successive writes. The latter distinction is needed for rebinding.
Flow = dict[str, set[int]]
Operations = dict[ast.stmt, list[FieldOperation]]
CONSTRUCTOR = "__init__"


def _flow(**outcomes: set[int]) -> Flow:
    return {name: outcomes.get(name, set()) for name in
            ("normal", "return", "raise", "break", "continue")}


def _merge(into: Flow, other: Flow) -> None:
    for name in into:
        into[name].update(other[name])


def _targets(target: ast.expr, field: str, receiver: str) -> bool:
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return target.value.id == receiver and target.attr == field
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_targets(item, field, receiver) for item in target.elts)
    return False


def _block(body: list[ast.stmt], states: set[int], field: str, receiver: str,
           rebindings: dict[tuple[int, int], ast.expr] | None = None,
           operations: Operations | None = None,
           deletions: dict[tuple[int, int], ast.expr] | None = None) -> Flow:
    result = _flow(normal=states.copy())
    for statement in body:
        next_ = _step(statement, result["normal"], field, receiver, rebindings,
                      operations, deletions)
        result["normal"] = next_["normal"]
        for name in ("return", "raise", "break", "continue"):
            result[name].update(next_[name])
    return result


def _loop(body: list[ast.stmt], orelse: list[ast.stmt] | None, states: set[int],
           field: str, *, receiver: str,
           rebindings: dict[tuple[int, int], ast.expr] | None = None,
           operations: Operations | None = None,
           deletions: dict[tuple[int, int], ast.expr] | None = None) -> Flow:
    # Zero iterations are possible; two rounds also expose repeated writes. Counts
    # saturate at two, so a third round cannot add a distinct write count.
    # None denotes a literal while True: only a break can leave it normally.
    exited: set[int] = states.copy() if orelse is not None else set()
    result = _flow()
    active = states.copy()
    for _ in range(2):
        step = _block(body, active, field, receiver, rebindings, operations, deletions)
        result["return"].update(step["return"])
        result["raise"].update(step["raise"])
        result["normal"].update(step["break"])
        active = step["normal"] | step["continue"]
        if orelse is not None:
            exited.update(active)
    if orelse is not None:
        _merge(result, _block(orelse, exited, field, receiver, rebindings, operations, deletions))
    return result


def _try(node: ast.Try | ast.TryStar, states: set[int], field: str, receiver: str,
          rebindings: dict[tuple[int, int], ast.expr] | None,
          operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None) -> Flow:
    body = _block(node.body, states, field, receiver, rebindings, operations, deletions)
    result = _flow()
    _merge(result, _block(node.orelse, body["normal"], field, receiver, rebindings,
                          operations, deletions))
    for name in ("return", "break", "continue"):
        result[name].update(body[name])
    if node.handlers:
        # A handled exception can occur before a write in the try body. Do not
        # assume an earlier assignment ran just because a later one is visible.
        for handler in node.handlers:
            _merge(result, _block(handler.body, states | body["raise"], field, receiver,
                                  rebindings, operations, deletions))
    else:
        result["raise"].update(body["raise"])
    return _finally(node.finalbody, result, field, receiver, rebindings, operations, deletions)


def _finally(body: list[ast.stmt], result: Flow, field: str, receiver: str,
             rebindings: dict[tuple[int, int], ast.expr] | None,
             operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None) -> Flow:
    if not body:
        return result
    finalized = _flow()
    for outcome, values in result.items():
        final = _block(body, values, field, receiver, rebindings, operations, deletions)
        finalized[outcome].update(final["normal"])
        for name in ("return", "raise", "break", "continue"):
            finalized[name].update(final[name])
    return finalized


def _simple(node: ast.stmt, states: set[int], field: str, receiver: str,
             rebindings: dict[tuple[int, int], ast.expr] | None,
             operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None
             ) -> Flow | None:
    if operations is not None:
        relevant = [op for op in operations.get(node, ()) if op.name == field]
        if relevant:
            states = _apply(relevant, states, rebindings, deletions)
            for kind, outcome in ((ast.Return, "return"), (ast.Raise, "raise"),
                                  (ast.Break, "break"), (ast.Continue, "continue")):
                if isinstance(node, kind):
                    return _flow(**{outcome: states})
            return _flow(normal=states)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            return _flow(normal=states.copy())
    if isinstance(node, ast.Assign):
        written = any(_targets(target, field, receiver) for target in node.targets)
    elif isinstance(node, ast.AnnAssign):
        written = node.value is not None and _targets(node.target, field, receiver)
    elif isinstance(node, ast.AugAssign) and _targets(node.target, field, receiver):
        # Reading a missing field before writing it cannot initialize the object.
        written = False
    elif isinstance(node, ast.Delete) and any(_targets(t, field, receiver) for t in node.targets):
        return _flow(normal={0 for count in states if count})
    else:
        for kind, outcome in ((ast.Return, "return"), (ast.Raise, "raise"),
                              (ast.Break, "break"), (ast.Continue, "continue")):
            if isinstance(node, kind):
                return _flow(**{outcome: states.copy()})
        return None
    if written and rebindings is not None and any(states):
        for store in _stores(node, receiver):
            if store.attr == field:
                rebindings[(store.lineno, store.col_offset)] = store
    return _flow(normal={min(2, count + 1) for count in states} if written else states.copy())


def _apply(events: list[FieldOperation], states: set[int],
           rebindings: dict[tuple[int, int], ast.expr] | None,
           deletions: dict[tuple[int, int], ast.expr] | None) -> set[int]:
    for op in events:
        if op.kind == "delete":
            if deletions is not None and any(states):
                deletions[(op.location.lineno, op.location.col_offset)] = op.location
            states = {0 for count in states if count}
        elif op.kind == "augment":
            states = states.copy()  # += cannot establish an absent binding.
        elif op.kind == "setdefault":
            states = {max(1, count) for count in states}
        else:
            if rebindings is not None and any(states):
                rebindings[(op.location.lineno, op.location.col_offset)] = op.location
            states = {min(2, count + 1) for count in states}
    return states


def _conditional(node: ast.If | ast.Match, states: set[int],
                  field: str, receiver: str,
                  rebindings: dict[tuple[int, int], ast.expr] | None,
                  operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None
                  ) -> Flow:
    if isinstance(node, ast.If):
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            branch = node.body if node.test.value else node.orelse
            return _block(branch, states, field, receiver, rebindings, operations, deletions)
        result = _block(node.body, states, field, receiver, rebindings, operations, deletions)
        _merge(result, _block(node.orelse, states, field, receiver, rebindings,
                              operations, deletions))
        return result
    result = _flow()
    exhaustive = False
    for case in node.cases:
        _merge(result, _block(case.body, states, field, receiver, rebindings,
                              operations, deletions))
        if (case.guard is None and isinstance(case.pattern, ast.MatchAs)
                and case.pattern.pattern is None):
            exhaustive = True
    if not exhaustive:
        result["normal"].update(states)
    return result


def _iteration(node: ast.For | ast.AsyncFor | ast.While, states: set[int],
                field: str, receiver: str,
                rebindings: dict[tuple[int, int], ast.expr] | None,
                operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None
                ) -> Flow:
    if isinstance(node, (ast.For, ast.AsyncFor)):
        # The iteration target itself can be a receiver field.
        target = _targets(node.target, field, receiver)
        body = ([ast.Assign(targets=[node.target], value=node.iter)] if target else []) + node.body
        return _loop(body, node.orelse, states, field, receiver=receiver, rebindings=rebindings,
                     operations=operations, deletions=deletions)
    if isinstance(node.test, ast.Constant) and node.test.value is False:
        return _block(node.orelse, states, field, receiver, rebindings, operations, deletions)
    orelse = (None if isinstance(node.test, ast.Constant) and node.test.value is True
              else node.orelse)
    return _loop(node.body, orelse, states, field, receiver=receiver, rebindings=rebindings,
                 operations=operations, deletions=deletions)


def _with(node: ast.With | ast.AsyncWith, states: set[int], field: str, receiver: str,
          rebindings: dict[tuple[int, int], ast.expr] | None,
          operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None
          ) -> Flow:
    if operations is not None:
        entered = _apply([event for event in operations.get(node, ()) if event.name == field],
                         states, rebindings, deletions)
    else:
        targets = sum(_targets(item.optional_vars, field, receiver)
                      for item in node.items if item.optional_vars is not None)
        entered = {min(2, count + targets) for count in states}
    if operations is None and targets and rebindings is not None and any(states):
        for item in node.items:
            if item.optional_vars and _targets(item.optional_vars, field, receiver):
                for store in _stores(ast.Assign(targets=[item.optional_vars], value=item.context_expr),
                                     receiver):
                    if store.attr == field:
                        rebindings[(store.lineno, store.col_offset)] = store
    result = _block(node.body, entered, field, receiver, rebindings, operations, deletions)
    # __exit__ may suppress an exception before the body reaches its write.
    result["normal"].update(entered)
    return result


def _step(node: ast.stmt, states: set[int], field: str, receiver: str,
          rebindings: dict[tuple[int, int], ast.expr] | None,
          operations: Operations | None, deletions: dict[tuple[int, int], ast.expr] | None
          ) -> Flow:
    if not states:
        result = _flow()
    elif not isinstance(node, (ast.If, ast.Match, ast.For, ast.AsyncFor, ast.While,
                               ast.Try, ast.TryStar, ast.With, ast.AsyncWith)) and (
            simple := _simple(node, states, field, receiver, rebindings,
                              operations, deletions)) is not None:
        result = simple
    elif isinstance(node, (ast.If, ast.Match)):
        if operations is not None:
            states = _apply([event for event in operations.get(node, ()) if event.name == field],
                            states, rebindings, deletions)
        result = _conditional(node, states, field, receiver, rebindings, operations, deletions)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        if operations is not None:
            states = _apply([event for event in operations.get(node, ()) if event.name == field],
                            states, rebindings, deletions)
        result = _iteration(node, states, field, receiver, rebindings, operations, deletions)
    elif isinstance(node, (ast.Try, ast.TryStar)):
        result = _try(node, states, field, receiver, rebindings, operations, deletions)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        result = _with(node, states, field, receiver, rebindings, operations, deletions)
    else:
        # Deferred bodies, helper calls and writes in expressions cannot prove
        # a direct constructor assignment.
        result = _flow(normal=states.copy())
    return result


def _stores(statement: ast.stmt, receiver: str) -> list[ast.Attribute]:
    pending: list[ast.AST] = [statement]
    stores: list[ast.Attribute] = []
    for current in pending:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if (isinstance(current, ast.Attribute) and isinstance(current.ctx, ast.Store)
                and isinstance(current.value, ast.Name) and current.value.id == receiver):
            stores.append(current)
        pending.extend(ast.iter_child_nodes(current))
    return sorted(stores, key=lambda item: (item.lineno, item.col_offset))


def _receiver(method: ast.stmt) -> str | None:
    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or any(
            isinstance(dec, ast.Name) and dec.id in {"classmethod", "staticmethod"}
            for dec in method.decorator_list):
        return None
    args = method.args.posonlyargs + method.args.args
    return args[0].arg if args else None


def written_fields(node: ast.ClassDef, tree: ast.Module | None = None) -> dict[str, ast.expr]:
    """Return first recognized receiver writes in a class's methods."""
    written: dict[str, ast.expr] = {}
    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (name := _receiver(method)) is None:
            continue
        if tree is not None:
            for events in field_operations(tree, method, name).values():
                for event in events:
                    if event.kind in {"write", "setdefault", "augment"}:
                        written.setdefault(event.name, event.location)
        else:
            for statement in method.body:
                for store in _stores(statement, name):
                    written.setdefault(store.attr, store)
    return written


def missing_fields(node: ast.ClassDef, declarations: dict[str, ast.expr],
                   generated: set[str], tree: ast.Module | None = None) -> dict[str, ast.expr]:
    """Locate stored fields without an owning constructor write on every exit.

    Args:
        node (ast.ClassDef): Class whose receiver fields are being analyzed.
        declarations (dict[str, ast.expr]): Source positions of declared fields.
        generated (set[str]): Fields assigned by a generated dataclass constructor.

    Returns:
        dict[str, ast.expr]: Uninitialized fields and their declaration or first write.
    """
    written: dict[str, ast.expr] = {**written_fields(node, tree), **declarations}
    constructor = next((method for method in reversed(node.body)
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and method.name == CONSTRUCTOR and _receiver(method) is not None), None)
    if constructor is None:
        return {field: location for field, location in written.items() if field not in generated}
    receiver = _receiver(constructor) or ""
    operations = field_operations(tree, constructor, receiver) if tree is not None else None
    missing: dict[str, ast.expr] = {}
    for field, location in written.items():
        exits = _block(constructor.body, {0}, field, receiver, operations=operations)
        if 0 in exits["normal"] | exits["return"]:
            missing[field] = location
    return missing


def rebound_fields(node: ast.ClassDef, fields: set[str], generated: set[str],
                   tree: ast.Module | None = None) -> tuple[dict[str, list[ast.expr]],
                                                             dict[str, list[ast.expr]]]:
    """Locate writes following bindings and deletions of bound fields."""
    result: dict[str, list[ast.expr]] = {}
    removed: dict[str, list[ast.expr]] = {}
    for field in fields:
        locations: dict[tuple[int, int], ast.expr] = {}
        deletions: dict[tuple[int, int], ast.expr] = {}
        for method in node.body:
            receiver = _receiver(method)
            if receiver is None or not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            initial = {0} if method.name == CONSTRUCTOR else {1}
            if method.name == CONSTRUCTOR and field in generated:
                initial = {1}
            operations = field_operations(tree, method, receiver) if tree is not None else None
            _ = _block(method.body, initial, field, receiver, locations, operations, deletions)
        if locations:
            result[field] = [locations[key] for key in sorted(locations)]
        if deletions:
            removed[field] = [deletions[key] for key in sorted(deletions)]
    return result, removed
