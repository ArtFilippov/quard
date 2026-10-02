"""Prove direct constructor writes on every path that creates a usable instance."""
from __future__ import annotations

import ast

# A count saturates at two: branches join as alternatives, whereas loop iterations
# can produce successive writes. The latter distinction is needed for rebinding.
Flow = dict[str, set[int]]
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


def _block(body: list[ast.stmt], states: set[int], field: str, receiver: str) -> Flow:
    result = _flow(normal=states.copy())
    for statement in body:
        next_ = _step(statement, result["normal"], field, receiver)
        result["normal"] = next_["normal"]
        for name in ("return", "raise", "break", "continue"):
            result[name].update(next_[name])
    return result


def _loop(body: list[ast.stmt], orelse: list[ast.stmt] | None, states: set[int],
          field: str, *, receiver: str) -> Flow:
    # Zero iterations are possible; two rounds also expose repeated writes. Counts
    # saturate at two, so a third round cannot add a distinct write count.
    # None denotes a literal while True: only a break can leave it normally.
    exited: set[int] = states.copy() if orelse is not None else set()
    result = _flow()
    active = states.copy()
    for _ in range(2):
        step = _block(body, active, field, receiver)
        result["return"].update(step["return"])
        result["raise"].update(step["raise"])
        result["normal"].update(step["break"])
        active = step["normal"] | step["continue"]
        if orelse is not None:
            exited.update(active)
    if orelse is not None:
        _merge(result, _block(orelse, exited, field, receiver))
    return result


def _try(node: ast.Try | ast.TryStar, states: set[int], field: str, receiver: str) -> Flow:
    body = _block(node.body, states, field, receiver)
    result = _flow()
    _merge(result, _block(node.orelse, body["normal"], field, receiver))
    for name in ("return", "break", "continue"):
        result[name].update(body[name])
    if node.handlers:
        # A handled exception can occur before a write in the try body. Do not
        # assume an earlier assignment ran just because a later one is visible.
        for handler in node.handlers:
            _merge(result, _block(handler.body, states | body["raise"], field, receiver))
    else:
        result["raise"].update(body["raise"])
    return _finally(node.finalbody, result, field, receiver)


def _finally(body: list[ast.stmt], result: Flow, field: str, receiver: str) -> Flow:
    if not body:
        return result
    finalized = _flow()
    for outcome, values in result.items():
        final = _block(body, values, field, receiver)
        finalized[outcome].update(final["normal"])
        for name in ("return", "raise", "break", "continue"):
            finalized[name].update(final[name])
    return finalized


def _simple(node: ast.stmt, states: set[int], field: str, receiver: str) -> Flow | None:
    if isinstance(node, ast.Assign):
        written = any(_targets(target, field, receiver) for target in node.targets)
    elif isinstance(node, ast.AnnAssign):
        written = node.value is not None and _targets(node.target, field, receiver)
    elif isinstance(node, ast.AugAssign) and _targets(node.target, field, receiver):
        # Reading a missing field before writing it cannot initialize the object.
        states = {count for count in states if count}
        written = True
    elif isinstance(node, ast.Delete) and any(_targets(t, field, receiver) for t in node.targets):
        return _flow(normal={0 for count in states if count})
    else:
        for kind, outcome in ((ast.Return, "return"), (ast.Raise, "raise"),
                              (ast.Break, "break"), (ast.Continue, "continue")):
            if isinstance(node, kind):
                return _flow(**{outcome: states.copy()})
        return None
    return _flow(normal={min(2, count + 1) for count in states} if written else states.copy())


def _conditional(node: ast.If | ast.Match, states: set[int],
                 field: str, receiver: str) -> Flow:
    if isinstance(node, ast.If):
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            branch = node.body if node.test.value else node.orelse
            return _block(branch, states, field, receiver)
        result = _block(node.body, states, field, receiver)
        _merge(result, _block(node.orelse, states, field, receiver))
        return result
    result = _flow()
    exhaustive = False
    for case in node.cases:
        _merge(result, _block(case.body, states, field, receiver))
        if (case.guard is None and isinstance(case.pattern, ast.MatchAs)
                and case.pattern.pattern is None):
            exhaustive = True
    if not exhaustive:
        result["normal"].update(states)
    return result


def _iteration(node: ast.For | ast.AsyncFor | ast.While, states: set[int],
               field: str, receiver: str) -> Flow:
    if isinstance(node, (ast.For, ast.AsyncFor)):
        # The iteration target itself can be a receiver field.
        target = _targets(node.target, field, receiver)
        body = ([ast.Assign(targets=[node.target], value=node.iter)] if target else []) + node.body
        return _loop(body, node.orelse, states, field, receiver=receiver)
    if isinstance(node.test, ast.Constant) and node.test.value is False:
        return _block(node.orelse, states, field, receiver)
    orelse = (None if isinstance(node.test, ast.Constant) and node.test.value is True
              else node.orelse)
    return _loop(node.body, orelse, states, field, receiver=receiver)


def _with(node: ast.With | ast.AsyncWith, states: set[int], field: str, receiver: str) -> Flow:
    targets = sum(_targets(item.optional_vars, field, receiver)
                  for item in node.items if item.optional_vars is not None)
    entered = {min(2, count + targets) for count in states}
    result = _block(node.body, entered, field, receiver)
    # __exit__ may suppress an exception before the body reaches its write.
    result["normal"].update(entered)
    return result


def _step(node: ast.stmt, states: set[int], field: str, receiver: str) -> Flow:
    if not states:
        result = _flow()
    elif (simple := _simple(node, states, field, receiver)) is not None:
        result = simple
    elif isinstance(node, (ast.If, ast.Match)):
        result = _conditional(node, states, field, receiver)
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        result = _iteration(node, states, field, receiver)
    elif isinstance(node, (ast.Try, ast.TryStar)):
        result = _try(node, states, field, receiver)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        result = _with(node, states, field, receiver)
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


def _written_fields(node: ast.ClassDef) -> dict[str, ast.Attribute]:
    written: dict[str, ast.Attribute] = {}
    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (name := _receiver(method)) is None:
            continue
        for statement in method.body:
            for store in _stores(statement, name):
                if store.attr not in written:
                    written[store.attr] = store
    return written


def missing_fields(node: ast.ClassDef, declarations: dict[str, ast.expr],
                   generated: set[str]) -> dict[str, ast.expr]:
    """Locate stored fields without an owning constructor write on every exit.

    Args:
        node (ast.ClassDef): Class whose receiver fields are being analyzed.
        declarations (dict[str, ast.expr]): Source positions of declared fields.
        generated (set[str]): Fields assigned by a generated dataclass constructor.

    Returns:
        dict[str, ast.expr]: Uninitialized fields and their declaration or first write.
    """
    written: dict[str, ast.expr] = {**_written_fields(node), **declarations}
    constructor = next((method for method in reversed(node.body)
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and method.name == CONSTRUCTOR and _receiver(method) is not None), None)
    if constructor is None:
        return {field: location for field, location in written.items() if field not in generated}
    receiver = _receiver(constructor) or ""
    missing: dict[str, ast.expr] = {}
    for field, location in written.items():
        exits = _block(constructor.body, {0}, field, receiver)
        if 0 in exits["normal"] | exits["return"]:
            missing[field] = location
    return missing
