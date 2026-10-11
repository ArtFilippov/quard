"""Resolve assembly calls and check the origins of their injected values."""
from __future__ import annotations

import ast

from quard.constructor_origins.origins import Environment, Origins
from quard.constructor_origins.origins_utils import UNKNOWN, merge, module_name


class LambdaSites:
    """Walk lexical scopes in source order, using proven call origins only."""

    def __init__(self, path: str, origins: Origins):
        self.path = path
        self.module = module_name(path)
        self.origins = origins
        self.diagnostics: list[dict[str, object]] = []
        self.reported: set[tuple[int, int, str]] = set()
        self.future: set[str] = set()
        self.lambdas: dict[str, ast.Lambda] = {}
        self.defaults: dict[int, tuple[Environment, Environment]] = {}
        self.active: set[str] = set()

    def report(self, node: ast.expr, symbol: str, message: str) -> None:
        """A nested injection can reach the same bad origin more than once."""
        key = (node.lineno, node.col_offset, symbol)
        if key not in self.reported:
            self.reported.add(key)
            self.diagnostics.append({
                "rule": "LAMBDA_INJECTION", "path": self.path, "line": node.lineno,
                "column": node.col_offset + 1, "symbol": symbol, "message": message})

    def origin_message(self, node: ast.expr, values: Environment) -> str:
        """Explain unresolved value evidence separately from forbidden syntax."""
        if isinstance(node, ast.Name):
            if node.id not in values:
                return f"Unresolved argument '{node.id}': no proven local value source."
            known = values[node.id]
            if "project-global" in known:
                return f"Unresolved argument '{node.id}': project module binding is not local assembly."
            if "project-import" in known:
                return f"Unresolved argument '{node.id}': imported project value was not assembled here."
            return f"Unresolved alias '{node.id}': an assignment path has no proven local origin."
        if isinstance(node, ast.Call):
            return "Unresolved argument call: its target or an argument has no proven origin."
        return ("Unresolved argument origin: assemble locally, use a parameter "
                "or an External value.")

    def unsupported(self, node: ast.expr) -> list[ast.expr]:
        """Return unsupported expression roots, preserving the most precise positions."""
        if isinstance(node, (ast.Constant, ast.Name)):
            return []
        if isinstance(node, ast.Attribute):
            return self.unsupported(node.value)
        if isinstance(node, ast.Call):
            children = [node.func, *node.args, *(keyword.value for keyword in node.keywords)]
            return [bad for child in children for bad in self.unsupported(child)]
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [bad for child in node.elts for bad in self.unsupported(child)]
        if isinstance(node, ast.Dict):
            children = [child for pair in zip(node.keys, node.values) for child in pair
                        if child is not None]
            return ([node] if any(key is None for key in node.keys) else
                    [bad for child in children for bad in self.unsupported(child)])
        if isinstance(node, ast.Lambda):
            defaults = (*node.args.defaults, *(value for value in node.args.kw_defaults
                                              if value is not None))
            return [bad for child in (*defaults, node.body)
                    for bad in self.unsupported(child)]
        return [node]

    def lambda_values(self, node: ast.Lambda, values: Environment) -> Environment:
        """Parameters shadow captures; later writes cannot prove captured values."""
        local = values.copy()
        for name in self.future:
            if name in local:
                local[name] = UNKNOWN
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else [])):
            local[arg.arg] = frozenset({"valid"})
        return local

    def ordered(self, origins: frozenset[str]) -> list[str]:
        """Keep diagnostics stable across alternative lambda bindings."""
        return sorted(origins, key=lambda origin: (
            self.lambdas[origin].lineno, self.lambdas[origin].col_offset)
            if origin in self.lambdas else (0, 0))

    def shapes(self, node: ast.AST, values: Environment, seen: set[str] | None = None) -> None:
        """Inspect every reachable lambda, including aliases and container elements."""
        seen = set() if seen is None else seen
        if isinstance(node, ast.Name):
            for origin in self.ordered(values.get(node.id, frozenset())):
                if origin in self.lambdas and origin not in seen:
                    self.shapes(self.lambdas[origin], values, seen | {origin})
        elif isinstance(node, ast.Lambda):
            _, at_definition = self.defaults.setdefault(id(node), ({}, values.copy()))
            local = self.lambda_values(node, values)
            for child in (*node.args.defaults, *(value for value in node.args.kw_defaults
                                                 if value is not None), node.body):
                for bad in self.unsupported(child):
                    self.report(bad, "lambda", f"Unsupported {type(bad).__name__} " +
                                "in injected lambda; use a wrapper class.")
                self.shapes(child, local if child is node.body else at_definition, seen)
            return
        for child in ast.iter_child_nodes(node):
            self.shapes(child, values, seen)

    def external(self, origins: frozenset[str]) -> bool:
        """Recognize explicit outside imports, including imported module aliases."""
        return bool(origins) and all(
            origin.startswith("external:") or origin == "typed-external" or
            origin.startswith("module:") and self.origins.external(origin[7:])
            for origin in origins)

    def unsupported_references(self, node: ast.expr, env: Environment,
                               values: Environment) -> list[ast.expr]:
        """Check references even when syntax forbids an expression as a whole."""
        callees = {id(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}
        bad: list[ast.expr] = []
        pending: list[ast.AST] = [node]
        while pending:
            child = pending.pop()
            if isinstance(child, ast.Lambda):
                bad.extend(self.issues(child, env, values))
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if id(child) not in callees:
                    bad.extend(self.issues(child, env, values))
            else:
                pending.extend(ast.iter_child_nodes(child))
        return bad

    def issues(self, node: ast.expr, env: Environment,
               values: Environment) -> list[ast.expr]:
        """Find unproved leaves; a join is safe only if every origin is safe."""
        if isinstance(node, ast.Constant):
            return []
        if isinstance(node, ast.Name):
            known = values.get(node.id)
            if known is None:
                return [] if self.external(self.origins.expression(node, env)) else [node]
            bad: list[ast.expr] = ([] if known <= {"valid", "external-import", *self.lambdas}
                                   else [node])
            for origin in self.ordered(known):
                if origin in self.active:
                    bad.append(node)
                elif origin in self.lambdas:
                    self.active.add(origin)
                    bad.extend(self.issues(self.lambdas[origin], env, values))
                    self.active.remove(origin)
            return bad
        if isinstance(node, ast.Attribute):
            return self.issues(node.value, env, values)
        if isinstance(node, ast.Call):
            target = self.origins.expression(node.func, env)
            proven = bool(target) and all(
                origin.startswith(("class:", "function:", "type:")) or
                self.external(frozenset({origin})) for origin in target)
            problems = [] if proven else [node]
            return problems + [bad for child in (*node.args, *(key.value for key in node.keywords))
                               for bad in self.issues(child, env, values)]
        if isinstance(node, ast.Lambda):
            default_env, default_values = self.defaults.setdefault(
                id(node), (env.copy(), values.copy()))
            defaults = (*node.args.defaults, *(value for value in node.args.kw_defaults
                                               if value is not None))
            bad = [issue for child in defaults for issue in (
                self.unsupported_references(child, default_env, default_values)
                if self.unsupported(child) else self.issues(child, default_env, default_values))]
            local = self.lambda_values(node, values)
            return bad + (self.unsupported_references(node.body, env, local)
                          if self.unsupported(node.body) else self.issues(node.body, env, local))
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [bad for child in node.elts for bad in self.issues(child, env, values)]
        if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
            return [bad for child in (*node.keys, *node.values) if child is not None
                    for bad in self.issues(child, env, values)]
        return [node]  # Unsupported expressions cannot establish safe origins.

    def value(self, node: ast.expr, env: Environment, values: Environment) -> frozenset[str]:
        """Preserve unsafe alternatives across aliases and branch joins."""
        if isinstance(node, ast.Lambda):
            token = f"lambda:{id(node)}"
            self.lambdas[token] = node
            self.defaults[id(node)] = (env.copy(), values.copy())
            return frozenset({token})
        if isinstance(node, ast.Name) and node.id in values:
            return values[node.id]
        if isinstance(node, ast.Name) and self.external(self.origins.expression(node, env)):
            return frozenset({"external-import"})
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            children: list[ast.expr] = (node.elts if not isinstance(node, ast.Dict) else
                                        [child for child in (*node.keys, *node.values)
                                         if child is not None])
            origins = frozenset(origin for child in children
                                for origin in self.value(child, env, values))
            if isinstance(node, ast.Dict) and None in node.keys:
                origins |= UNKNOWN
            return origins or frozenset({"valid"})
        return frozenset({"valid"}) if not self.issues(node, env, values) else UNKNOWN

    def writes(self, statements: list[ast.stmt]) -> set[str]:
        """Find later writes in this lexical scope, without entering deferred bodies."""
        names: set[str] = set()

        def visit(node: ast.AST) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                return
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
                return
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    names.add(child.id)
                else:
                    visit(child)

        for statement in statements:
            visit(statement)
        return names

    def expression(self, node: ast.AST, env: Environment, values: Environment) -> None:
        if isinstance(node, ast.Call):
            target = self.origins.expression(node.func, env)
            if target and all(origin.startswith(("class:", "external:")) or
                              origin == "typed-external" or
                              origin.startswith("module:") and self.origins.external(origin[7:])
                              for origin in target):
                for child in (*node.args, *(keyword.value for keyword in node.keywords)):
                    self.shapes(child, values)
                    for bad in self.issues(child, env, values):
                        self.report(bad, "argument", self.origin_message(bad, values))
        if isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *(
                    value for value in node.args.kw_defaults if value is not None)):
                self.expression(default, env, values)
            return
        for child in ast.iter_child_nodes(node):
            self.expression(child, env, values)

    def suite(self, statements: list[ast.stmt], env: Environment,
              values: Environment | None = None, *, function: bool = False
              ) -> tuple[Environment, Environment]:
        env = env.copy()
        values = values.copy() if values is not None else {}
        enclosing_future = self.future
        for index, node in enumerate(statements):
            self.future = enclosing_future | self.writes(statements[index + 1:])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for expression in (*node.decorator_list, *node.args.defaults,
                                   *(value for value in node.args.kw_defaults if value is not None)):
                    self.expression(expression, env, values)
                local = env.copy()
                # Module bindings are not local to a function. Explicit External
                # imports keep their category; nested functions keep captured locals.
                captured = (values.copy() if function else {
                    name: origin if origin == {"external-import"} else frozenset({"project-global"})
                    for name, origin in values.items()})
                if function:
                    for name in self.writes(statements[index + 1:]):
                        if name in captured:
                            captured[name] = UNKNOWN
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                            *([node.args.vararg] if node.args.vararg else []),
                            *([node.args.kwarg] if node.args.kwarg else [])):
                    local[arg.arg] = (self.origins.annotation(arg.annotation, env)
                                      if arg.annotation else UNKNOWN)
                    captured[arg.arg] = frozenset({"valid"})
                saved_future = self.future
                self.future = set()
                self.suite(node.body, local, captured, function=True)
                self.future = saved_future
            elif isinstance(node, ast.ClassDef):
                for expression in (*node.bases, *node.decorator_list,
                                   *(item.value for item in node.keywords)):
                    self.expression(expression, env, values)
                self.suite(node.body, env, values, function=function)
            elif isinstance(node, ast.If):
                self.expression(node.test, env, values)
                yes, yes_values = self.suite(node.body, env, values, function=function)
                no, no_values = self.suite(node.orelse, env, values, function=function)
                env, values = merge(yes, no), merge(yes_values, no_values)
                continue
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                self.expression(node.iter if isinstance(node, (ast.For, ast.AsyncFor))
                                else node.test, env, values)
                branch = self.origins.suite([node], env, self.module)
                _, body_values = self.suite(node.body, branch, values, function=function)
                _, else_values = self.suite(node.orelse, branch, values, function=function)
                values = merge(values, body_values, else_values)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    self.expression(item.context_expr, env, values)
                bound = values.copy()
                for item in node.items:
                    if item.optional_vars:
                        for child in ast.walk(item.optional_vars):
                            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                                bound[child.id] = UNKNOWN
                _, body_values = self.suite(node.body, self.origins.suite([node], env, self.module),
                                            bound, function=function)
                values = merge(values, body_values)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                _, body_values = self.suite(node.body, env, values, function=function)
                alternatives = [values, body_values]
                for handler in node.handlers:
                    branch = env.copy()
                    handler_values = values.copy()
                    if handler.name:
                        branch[handler.name] = UNKNOWN
                        handler_values[handler.name] = UNKNOWN
                    _, handled = self.suite(handler.body, branch, handler_values, function=function)
                    alternatives.append(handled)
                _, otherwise = self.suite(node.orelse, env, body_values, function=function)
                alternatives.append(otherwise)
                values = merge(*alternatives)
                _, values = self.suite(node.finalbody, env, values, function=function)
            elif isinstance(node, ast.Match):
                self.expression(node.subject, env, values)
                alternatives = [values]
                for case in node.cases:
                    branch = env.copy()
                    case_values = values.copy()
                    for child in ast.walk(case.pattern):
                        if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                            branch[child.name] = UNKNOWN
                            case_values[child.name] = UNKNOWN
                        if isinstance(child, ast.MatchMapping) and child.rest:
                            branch[child.rest] = UNKNOWN
                            case_values[child.rest] = UNKNOWN
                    if case.guard:
                        self.expression(case.guard, branch, case_values)
                    _, matched = self.suite(case.body, branch, case_values, function=function)
                    alternatives.append(matched)
                values = merge(*alternatives)
            else:
                for child in ast.iter_child_nodes(node):
                    self.expression(child, env, values)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, origin in self.origins.import_bindings(node, self.module).items():
                    values[name] = (frozenset({"external-import"}) if self.external(origin)
                                    else frozenset({"project-import"}))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                source = node.value
                resolved = self.value(source, env, values) if source else UNKNOWN
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = resolved
                    else:
                        for child in ast.walk(target):
                            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                                values[child.id] = UNKNOWN
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                values[node.target.id] = UNKNOWN
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                values[node.name] = UNKNOWN
            env = self.origins.suite([node], env, self.module)
            if (function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not node.decorator_list):
                env[node.name] = frozenset({f"function:{self.module}.{node.name}"})
        self.future = enclosing_future
        return env, values
