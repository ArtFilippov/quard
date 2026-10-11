"""Resolve direct injection sites and inspect their lambda bodies."""
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
        return [node]

    def expression(self, node: ast.AST, env: Environment) -> None:
        if isinstance(node, ast.Call):
            target = self.origins.expression(node.func, env)
            if target and all(origin.startswith(("class:", "external:")) or
                              origin == "typed-external" for origin in target):
                for child in (*node.args, *(keyword.value for keyword in node.keywords)):
                    if isinstance(child, ast.Lambda):
                        for bad in self.unsupported(child.body):
                            self.diagnostics.append({
                                "rule": "LAMBDA_INJECTION", "path": self.path, "line": bad.lineno,
                                "column": bad.col_offset + 1, "symbol": "lambda",
                                "message": f"Unsupported {type(bad).__name__} in directly injected "
                                           "lambda; use a wrapper class."})
        if isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *(
                    value for value in node.args.kw_defaults if value is not None)):
                self.expression(default, env)
            return
        for child in ast.iter_child_nodes(node):
            self.expression(child, env)

    def suite(self, statements: list[ast.stmt], env: Environment) -> Environment:
        env = env.copy()
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for expression in (*node.decorator_list, *node.args.defaults,
                                   *(value for value in node.args.kw_defaults if value is not None)):
                    self.expression(expression, env)
                local = env.copy()
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                            *([node.args.vararg] if node.args.vararg else []),
                            *([node.args.kwarg] if node.args.kwarg else [])):
                    local[arg.arg] = (self.origins.annotation(arg.annotation, env)
                                      if arg.annotation else UNKNOWN)
                self.suite(node.body, local)
            elif isinstance(node, ast.ClassDef):
                for expression in (*node.bases, *node.decorator_list,
                                   *(item.value for item in node.keywords)):
                    self.expression(expression, env)
                self.suite(node.body, env)
            elif isinstance(node, ast.If):
                self.expression(node.test, env)
                env = merge(self.suite(node.body, env), self.suite(node.orelse, env))
                continue
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                self.expression(node.iter if isinstance(node, (ast.For, ast.AsyncFor))
                                else node.test, env)
                branch = self.origins.suite([node], env, self.module)
                self.suite(node.body, branch)
                self.suite(node.orelse, branch)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    self.expression(item.context_expr, env)
                self.suite(node.body, self.origins.suite([node], env, self.module))
            elif isinstance(node, (ast.Try, ast.TryStar)):
                self.suite(node.body, env)
                for handler in node.handlers:
                    branch = env.copy()
                    if handler.name:
                        branch[handler.name] = UNKNOWN
                    self.suite(handler.body, branch)
                self.suite(node.orelse, env)
                self.suite(node.finalbody, env)
            elif isinstance(node, ast.Match):
                self.expression(node.subject, env)
                for case in node.cases:
                    branch = env.copy()
                    for child in ast.walk(case.pattern):
                        if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                            branch[child.name] = UNKNOWN
                        if isinstance(child, ast.MatchMapping) and child.rest:
                            branch[child.rest] = UNKNOWN
                    if case.guard:
                        self.expression(case.guard, branch)
                    self.suite(case.body, branch)
            else:
                for child in ast.iter_child_nodes(node):
                    self.expression(child, env)
            env = self.origins.suite([node], env, self.module)
        return env
