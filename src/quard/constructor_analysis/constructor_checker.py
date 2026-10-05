"""Check eager syntax in discovered Python constructor bodies without executing source."""
from __future__ import annotations

import ast
import sys

from quard.constructor_analysis.constructor_analysis import ConstructorAnalysis
from quard.constructor_origins.origins import Origins, UNKNOWN, Environment, merge, module_name
from quard.constructor_analysis.checker_utils import (EXECUTION_ERROR, FUNCTIONS, PARSE_ERROR,
                                                     RULE, analyze, bindings, main)


class ConstructorChecker(ConstructorAnalysis):
    """Keep syntax decisions local to a constructor and its lexical class."""

    def __init__(self, path: str, origins: Origins):
        super().__init__()
        self.path = path
        self.origins = origins
        self.env: Environment = {}
        self.diagnostics: list[dict[str, object]] = []
        self.symbol = ""
        self.receiver: str | None = None
        self.local_bindings: set[str] = set()
        self.bases: set[str] = set()
        self.owner: frozenset[str] = frozenset()
        self.fields: Environment = {}
        self.dynamic_functions: dict[str, tuple[str, ast.FunctionDef | ast.AsyncFunctionDef,
                                                 Environment]] = {}
        self.dynamic_lambdas: dict[str, tuple[str, ast.Lambda, Environment]] = {}
        self.active_decorators: set[str] = set()
        self.stage_returns: list[frozenset[str]] | None = None
        self.stage_application = False
        self.reported_nodes: set[int] = set()

    def reject(self, node: ast.expr | ast.stmt | ast.pattern, reason: str) -> None:
        if id(node) in self.reported_nodes:
            return
        self.reported_nodes.add(id(node))
        self.diagnostics.append({"rule": RULE, "path": self.path,
                                 "line": node.lineno, "column": node.col_offset + 1,
                                 "symbol": self.symbol,
                                 "message": f"{self.symbol}: {reason}."})

    def origin(self, node: ast.expr) -> frozenset[str]:
        if isinstance(node, ast.Lambda):
            key = f"{self.path}:{node.lineno}:{node.col_offset}"
            return frozenset({f"function:{key}"}) if key in self.dynamic_lambdas else UNKNOWN
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == self.receiver):
            return self.fields.get(node.attr, UNKNOWN)
        if isinstance(node, ast.Attribute) and self.origin(node.value) == {"typed-external"}:
            return frozenset({"external:member"})
        return self.origins.expression(node, self.env)

    def definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Evaluate only expressions executed while defining a deferred function."""
        decorators = [self.decorator_value(decorator) for decorator in node.decorator_list]
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.expr(default)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    node.args.vararg, node.args.kwarg):
            if arg is not None and arg.annotation is not None:
                self.expr(arg.annotation)
        if node.returns is not None:
            self.expr(node.returns)
        if node.decorator_list:
            for decorator, origin in reversed(list(zip(node.decorator_list, decorators))):
                self.apply_decorator(origin, decorator)
            self.env[node.name] = UNKNOWN
        else:
            key = f"{self.path}:{node.lineno}:{node.col_offset}"
            self.dynamic_functions[key] = (self.path, node, self.env.copy())
            self.env[node.name] = frozenset({f"function:{key}"})

    def decorator_value(self, node: ast.expr) -> frozenset[str]:
        """Factory execution is eager, but ordinary decorator expression rules apply."""
        if isinstance(node, ast.Call):
            target = self.decorator_value(node.func)
            if not self.parent(node) and not (target and all(
                item.startswith(("function:", "class:", "type:", "external:")) or
                item in {"builtin:isinstance", "builtin:issubclass", "builtin:hasattr",
                         "builtin:callable"} for item in target)):
                self.reject(node, "unproven or disallowed eager call")
            for arg in node.args:
                self.expr(arg)
            for keyword in node.keywords:
                self.expr(keyword.value)
            results: set[str] = set()
            for item in target:
                if item.startswith("function:"):
                    results.update(self.execute_decorator(item))
                elif item.startswith("external:"):
                    results.add("external:result")
                elif item.startswith("class:"):
                    results.add(f"instance:{item[6:]}")
                else:
                    results.add("unknown")
            return frozenset(results) if results else UNKNOWN
        self.expr(node)
        return self.origin(node)

    def apply_decorator(self, origins: frozenset[str], node: ast.expr) -> None:
        if not origins or "unknown" in origins:
            self.reject(node, "unresolved decorator application")
        for origin in origins:
            if origin.startswith(("external:", "class:")):
                # A project class application is construction; its __init__ is
                # checked independently as a constructor in discovered source.
                continue
            if origin.startswith("function:"):
                if self.execute_decorator(origin, application=True) == UNKNOWN:
                    self.reject(node, "unresolved decorator application")
            elif origin.startswith("instance:"):
                method = self.origins.methods.get(f"{origin[9:]}.__call__")
                if method is None:
                    self.reject(node, "unresolved decorator application")
                else:
                    if self.execute_decorator(f"callable:{origin[9:]}", method,
                                              application=True) == UNKNOWN:
                        self.reject(node, "unresolved decorator application")
            elif origin != "unknown":
                self.reject(node, "unresolved decorator application")

    def execute_decorator(self, origin: str, method: tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]
                          | None = None, *, application: bool = False) -> frozenset[str]:
        """Check a proven project's application/factory frame, leaving returned bodies deferred."""
        if origin in self.active_decorators:
            return UNKNOWN
        key = origin.removeprefix("function:")
        if method is not None:
            module, function = method
            path = self.origins.module_paths[module]
            env = self.origins.environment(module).copy()
            receiver = function.args.args[0].arg if function.args.args else None
        elif key in self.dynamic_functions:
            path, function, env = self.dynamic_functions[key]
            receiver = None
        elif key in self.origins.functions:
            module, function = self.origins.functions[key]
            path = self.origins.module_paths[module]
            env = self.origins.environment(module).copy()
            receiver = None
        else:
            if key not in self.dynamic_lambdas:
                return UNKNOWN
            path, function, env = self.dynamic_lambdas[key]
            receiver = None
        if isinstance(function, ast.AsyncFunctionDef):
            # Calling an async function creates a coroutine; its body stays deferred.
            return frozenset({"function:coroutine"}) if application else UNKNOWN
        saved = (self.path, self.env, self.receiver, self.fields,
                 self.stage_returns, self.stage_application)
        self.active_decorators.add(origin)
        try:
            self.path, self.env, self.receiver = path, env.copy(), receiver
            self.fields = {}
            self.stage_returns = []
            self.stage_application = application
            for arg in (*function.args.posonlyargs, *function.args.args,
                        *function.args.kwonlyargs, function.args.vararg, function.args.kwarg):
                if arg is not None:
                    self.env[arg.arg] = UNKNOWN
            if application:
                positionals = function.args.posonlyargs + function.args.args
                index = 1 if method is not None else 0
                if len(positionals) > index:
                    self.env[positionals[index].arg] = frozenset({"function:decorated"})
            if not isinstance(function, ast.Lambda):
                for name in bindings(function.body):
                    self.env[name] = UNKNOWN
            if isinstance(function, ast.Lambda):
                self.expr(function.body)
                result = self.origin(function.body)
            else:
                self.suite(function.body, docstring=True)
                result = (frozenset[str]().union(*self.stage_returns)
                          if self.stage_returns else UNKNOWN)
            return result
        finally:
            (self.path, self.env, self.receiver, self.fields,
             self.stage_returns, self.stage_application) = saved
            self.active_decorators.remove(origin)

    def parent(self, node: ast.Call) -> bool:
        """Only direct super initialization and a declared base's direct initializer."""
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "__init__":
            return False
        owner = node.func.value
        if (self.receiver is not None and isinstance(owner, ast.Call)
                and self.origin(owner.func) == {"builtin:super"}
                and not owner.keywords):
            if not owner.args:
                return True
            return (len(owner.args) == 2 and self.origin(owner.args[0]) == self.owner
                    and isinstance(owner.args[1], ast.Name)
                    and owner.args[1].id == self.receiver)
        return (self.receiver is not None and self.origin(owner) <= self.bases
                and self.origin(owner) != UNKNOWN
                and bool(node.args)
                and isinstance(node.args[0], ast.Name) and node.args[0].id == self.receiver)

    def call(self, node: ast.Call, *, standalone: bool = False) -> None:
        origins = self.origin(node.func)
        allowed = self.parent(node) or (not standalone and bool(origins) and all(
            item.startswith(("class:", "type:", "external:")) or
            item in {"builtin:isinstance", "builtin:issubclass", "builtin:hasattr",
                     "builtin:callable"} for item in origins))
        if not allowed:
            self.reject(node, "unproven or disallowed eager call")
        # A parent call's inner super(...) is syntax for selecting the parent, not
        # an independent constructor or an arbitrary callable.
        if self.parent(node) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Call):
                for arg in owner.args:
                    self.expr(arg)
        else:
            self.expr(node.func)
        for arg in node.args:
            self.expr(arg)
        for keyword in node.keywords:
            self.expr(keyword.value)

    def children(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.expr(child)
            elif isinstance(child, (ast.keyword, ast.comprehension)):
                self.children(child)

    def expr(self, node: ast.expr) -> None:
        if isinstance(node, ast.Call):
            self.call(node)
        elif isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.expr(default)
            # Lambda body is deferred until invocation.
            key = f"{self.path}:{node.lineno}:{node.col_offset}"
            self.dynamic_lambdas[key] = (self.path, node, self.env.copy())
        elif isinstance(node, (ast.Constant, ast.Name)):
            return
        elif isinstance(node, (ast.Attribute, ast.Subscript, ast.Slice,
                               ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Starred,
                               ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
                               ast.IfExp, ast.JoinedStr, ast.FormattedValue)):
            self.children(node)
        else:
            self.reject(node, f"forbidden {type(node).__name__}")
            self.children(node)

    def target(self, node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            if node.id == self.receiver:
                self.reject(node, "receiver rebinding")
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id != self.receiver:
                self.reject(node, "assignment outside local or direct receiver state")
                self.expr(node.value)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                self.target(element)
        elif isinstance(node, ast.Starred):
            self.target(node.value)
        else:
            self.reject(node, "assignment outside local or direct receiver state")
            self.children(node)

    def pattern(self, node: ast.pattern) -> None:
        if not isinstance(node, (ast.MatchValue, ast.MatchSingleton, ast.MatchSequence,
                                 ast.MatchMapping, ast.MatchClass, ast.MatchStar,
                                 ast.MatchAs, ast.MatchOr)):
            self.reject(node, f"forbidden {type(node).__name__} pattern")
        if isinstance(node, ast.MatchAs):
            if node.name == self.receiver:
                self.reject(node, "receiver rebinding in pattern")
        if isinstance(node, ast.MatchStar) and node.name == self.receiver:
            self.reject(node, "receiver rebinding in pattern")
        if isinstance(node, ast.MatchMapping) and node.rest == self.receiver:
            self.reject(node, "receiver rebinding in pattern")
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.pattern):
                self.pattern(child)
            elif isinstance(child, ast.expr):
                self.expr(child)

    def statement(self, node: ast.stmt) -> None:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self.target(target)
            self.expr(node.value)
            value = self.origin(node.value)
            for target in node.targets:
                self.bind(target, value)
        elif isinstance(node, ast.AnnAssign):
            self.target(node.target)
            self.expr(node.annotation)
            if node.value is not None:
                self.expr(node.value)
            annotated = self.origins.annotation(node.annotation, self.env)
            value = self.origin(node.value) if node.value is not None else UNKNOWN
            self.bind(node.target, annotated if annotated != UNKNOWN else value)
        elif isinstance(node, ast.AugAssign):
            self.target(node.target)
            self.expr(node.value)
            self.bind(node.target, UNKNOWN)
        elif isinstance(node, ast.If):
            self.expr(node.test)
            before, fields = self.env.copy(), self.fields.copy()
            self.suite(node.body)
            body, body_fields = self.env, self.fields
            self.env, self.fields = before, fields
            self.suite(node.orelse)
            self.env = merge(body, self.env)
            self.fields = merge(body_fields, self.fields)
        elif isinstance(node, ast.Match):
            self.expr(node.subject)
            before, fields = self.env.copy(), self.fields.copy()
            final = node.cases[-1].pattern if node.cases else None
            exhaustive = (isinstance(final, ast.MatchAs) and final.name is None
                          and final.pattern is None and node.cases[-1].guard is None)
            branches = [] if exhaustive else [before]
            field_branches = [] if exhaustive else [fields]
            for case in node.cases:
                self.env, self.fields = before.copy(), fields.copy()
                self.pattern(case.pattern)
                for child in ast.walk(case.pattern):
                    if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                        self.env[child.name] = UNKNOWN
                    if isinstance(child, ast.MatchMapping) and child.rest:
                        self.env[child.rest] = UNKNOWN
                if case.guard is not None:
                    self.expr(case.guard)
                self.suite(case.body)
                branches.append(self.env)
                field_branches.append(self.fields)
            self.env, self.fields = merge(*branches), merge(*field_branches)
        elif isinstance(node, ast.Raise):
            if node.exc is not None:
                self.expr(node.exc)
            if node.cause is not None:
                self.expr(node.cause)
        elif isinstance(node, ast.Assert):
            self.expr(node.test)
            if node.msg is not None:
                self.expr(node.msg)
        elif isinstance(node, ast.Pass):
            return
        elif isinstance(node, ast.Return) and self.stage_returns is not None:
            if node.value is not None:
                self.expr(node.value)
                value = self.origin(node.value)
                self.stage_returns.append(value)
                if self.stage_application and not (value and all(
                    item.startswith("function:") for item in value)):
                    self.reject(node, "decorator application must return a function")
            else:
                self.stage_returns.append(UNKNOWN)
                if self.stage_application:
                    self.reject(node, "decorator application must return a function")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            self.call(node.value, standalone=True)
        elif isinstance(node, FUNCTIONS):
            self.definition(node)
        else:
            self.reject(node, f"forbidden {type(node).__name__} statement")
            if isinstance(node, ast.Expr):
                self.expr(node.value)
            elif not isinstance(node, ast.ClassDef):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        self.statement(child)
                    elif isinstance(child, ast.expr):
                        self.expr(child)
                    elif isinstance(child, ast.withitem):
                        self.expr(child.context_expr)
                        if child.optional_vars is not None:
                            self.target(child.optional_vars)
                    elif isinstance(child, ast.ExceptHandler):
                        if child.type is not None:
                            self.expr(child.type)
                        self.suite(child.body)
            # Forbidden control flow never supplies trusted provenance.
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    self.env[child.id] = UNKNOWN

    def bind(self, target: ast.expr, value: frozenset[str]) -> None:
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == self.receiver):
            self.fields[target.attr] = value
        else:
            self.origins.bind(target, value, self.env)

    def suite(self, statements: list[ast.stmt], *, docstring: bool = False) -> None:
        for index, statement in enumerate(statements):
            if (docstring and index == 0 and isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)):
                continue
            self.statement(statement)

    def constructor(self, method: ast.FunctionDef | ast.AsyncFunctionDef,
                    qualified: str, cls: ast.ClassDef) -> None:
        self.symbol = qualified + ".__init__"
        self.env = self.origins.environment(module_name(self.path)).copy()
        self.owner = frozenset({f"class:{module_name(self.path)}.{qualified}"})
        self.bases = {item for base in cls.bases for item in self.origin(base)
                      if item.startswith(("class:", "external:"))}
        self.fields = {}
        self.dynamic_functions = {}
        self.dynamic_lambdas = {}
        self.active_decorators = set()
        self.stage_returns = None
        self.stage_application = False
        self.reported_nodes = set()
        positionals = method.args.posonlyargs + method.args.args
        self.receiver = (positionals[0].arg if len(positionals) > len(method.args.defaults) else None)
        self.local_bindings = bindings(method.body) | {
            arg.arg for arg in (*positionals, *method.args.kwonlyargs,
                             method.args.vararg, method.args.kwarg) if arg is not None}
        for name in self.local_bindings:
            self.env[name] = UNKNOWN
        for arg in (*positionals, *method.args.kwonlyargs):
            if arg.annotation is not None:
                self.env[arg.arg] = self.origins.annotation(arg.annotation,
                    self.origins.environment(module_name(self.path)))
        self.suite(method.body, docstring=True)


if __name__ == "__main__":
    sys.exit(main())
