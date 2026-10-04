"""Syntactic call origins; never load imported code or third-party declarations."""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

from eryx.constructor_origins.constructor_origins import ConstructorOrigins


UNKNOWN = frozenset({"unknown"})
Origin = frozenset[str]
Environment = dict[str, Origin]


def merge(*environments: Environment) -> Environment:
    """Missing bindings at a join are unknown, not evidence of a builtin."""
    return {name: frozenset().union(*(env.get(name, UNKNOWN) for env in environments))
            for name in set().union(*(set(env) for env in environments))}


def builtin(name: str) -> Origin:
    value = vars(builtins).get(name)
    if isinstance(value, type) and name != "super":
        return frozenset({f"type:{name}"})
    if name in {"isinstance", "issubclass", "hasattr", "callable", "super"}:
        return frozenset({f"builtin:{name}"})
    return UNKNOWN


def module_name(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if parts[0] == "src":
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


class Origins(ConstructorOrigins):
    """Resolve discovered customer modules and explicit external imports."""

    def __init__(self, trees: dict[str, ast.Module]):
        self.trees = {module_name(path): tree for path, tree in trees.items()}
        self.module_paths = {module_name(path): path for path in trees}
        self.packages = {module_name(path) for path in trees if Path(path).stem == "__init__"}
        self.roots = {module.split(".")[0] for module in self.trees if module}
        self.classes: set[str] = set()
        self.functions: dict[str, tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        self.methods: dict[str, tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for module, tree in self.trees.items():
            def collect(nodes: list[ast.stmt], owner: str) -> None:
                for statement in nodes:
                    if isinstance(statement, ast.ClassDef):
                        qualified = f"{owner}.{statement.name}"
                        self.classes.add(qualified)
                        collect(statement.body, qualified)
                    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        key = f"{owner}.{statement.name}"
                        if owner == module:
                            self.functions[key] = (module, statement)
                        else:
                            self.methods[key] = (module, statement)
            collect(tree.body, module)
        self.cache: dict[str, Environment] = {}
        self.active: set[str] = set()

    def external(self, module: str) -> bool:
        return bool(module) and module.split(".")[0] not in self.roots

    def imported(self, target: str) -> Origin:
        """Follow customer exports, stopping at unknowns and import cycles."""
        if target == "builtins":
            return frozenset({"module:builtins"})
        if target.startswith("builtins."):
            return builtin(target[len("builtins."):])
        if self.external(target):
            return frozenset({f"external:{target}"})
        if target in self.trees:
            return frozenset({f"module:{target}"})
        if target in self.classes:
            parent, _, child = target.rpartition(".")
            if parent not in self.trees and self.imported(parent) == {f"class:{parent}"}:
                return frozenset({f"class:{parent}.{child}"})
        module, _, member = target.rpartition(".")
        if module in self.trees:
            return self.environment(module).get(member, UNKNOWN)
        return UNKNOWN

    def import_bindings(self, node: ast.Import | ast.ImportFrom,
                        module: str) -> Environment:
        result: Environment = {}
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = alias.name if alias.asname else alias.name.split(".")[0]
                result[alias.asname or target] = (frozenset({f"module:{target}"})
                    if self.external(target) else self.imported(target))
        else:
            package = module if module in self.packages else module.rpartition(".")[0]
            parts = package.split(".") if package else []
            prefix = parts[:len(parts) - node.level + 1] if node.level else []
            target = ".".join((*prefix, node.module)) if node.module else ".".join(prefix)
            for alias in node.names:
                if alias.name != "*":
                    result[alias.asname or alias.name] = self.imported(f"{target}.{alias.name}")
        return result

    def expression(self, node: ast.expr, env: Environment) -> Origin:
        if isinstance(node, ast.Name):
            return env.get(node.id, UNKNOWN)
        if isinstance(node, ast.Attribute):
            result: set[str] = set()
            for origin in self.expression(node.value, env):
                if origin.startswith("module:"):
                    result.update(self.imported(f"{origin[7:]}.{node.attr}"))
                elif origin.startswith("class:") and f"{origin[6:]}.{node.attr}" in self.classes:
                    result.add(f"class:{origin[6:]}.{node.attr}")
                elif origin.startswith("external:") or origin == "typed-external":
                    result.add("external:member")
                else:
                    result.add("unknown")
            return frozenset(result)
        if isinstance(node, ast.IfExp):
            return self.expression(node.body, env) | self.expression(node.orelse, env)
        if isinstance(node, ast.Call):
            origins = self.expression(node.func, env)
            if origins and all(item.startswith("class:") for item in origins):
                return frozenset({f"instance:{item[6:]}" for item in origins})
        return UNKNOWN

    def annotation(self, node: ast.expr, env: Environment) -> Origin:
        """Only an exact externally imported type expression proves a receiver."""
        origin = self.expression(node, env)
        return (frozenset({"typed-external"}) if origin and
                all(item.startswith("external:") for item in origin) else UNKNOWN)

    def bind(self, target: ast.expr, origin: Origin, env: Environment) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = origin
        elif isinstance(target, ast.Starred):
            self.bind(target.value, UNKNOWN, env)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for child in target.elts:
                self.bind(child, UNKNOWN, env)

    def suite(self, statements: list[ast.stmt], env: Environment,
              module: str = "") -> Environment:
        """Track simple assignments and conservatively join control-flow exits."""
        env = env.copy()
        for node in statements:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                env.update(self.import_bindings(node, module))
            elif isinstance(node, ast.ClassDef):
                env[node.name] = frozenset({f"class:{module}.{node.name}"})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"{module}.{node.name}"
                env[node.name] = (frozenset({f"function:{key}"})
                                  if key in self.functions and not node.decorator_list else UNKNOWN)
            elif isinstance(node, ast.Assign):
                value = self.expression(node.value, env)
                for target in node.targets:
                    self.bind(target, value, env)
            elif isinstance(node, ast.AnnAssign):
                value = self.expression(node.value, env) if node.value else UNKNOWN
                annotated = self.annotation(node.annotation, env)
                self.bind(node.target, annotated if annotated != UNKNOWN else value, env)
            elif isinstance(node, ast.AugAssign):
                self.bind(node.target, UNKNOWN, env)
            elif isinstance(node, ast.If):
                env = merge(self.suite(node.body, env, module),
                            self.suite(node.orelse, env, module))
            elif isinstance(node, ast.Match):
                final = node.cases[-1].pattern if node.cases else None
                exhaustive = (isinstance(final, ast.MatchAs) and final.name is None
                              and final.pattern is None and node.cases[-1].guard is None)
                branches = [] if exhaustive else [env]
                for case in node.cases:
                    branch = env.copy()
                    for child in ast.walk(case.pattern):
                        if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                            branch[child.name] = UNKNOWN
                        if isinstance(child, ast.MatchMapping) and child.rest:
                            branch[child.rest] = UNKNOWN
                    branches.append(self.suite(case.body, branch, module))
                env = merge(*branches)
            else:
                # Unsupported control flow cannot confer proven origins.
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                        env[child.id] = UNKNOWN
        return env

    def environment(self, module: str) -> Environment:
        if module in self.cache:
            return self.cache[module]
        if module in self.active:
            return {}
        self.active.add(module)
        try:
            tree = self.trees[module]
            # A module's own builtins are available unless lexically shadowed.
            env = {name: builtin(name) for name in vars(builtins)}
            result = self.suite(tree.body, env, module)
            self.cache[module] = result
            return result
        finally:
            self.active.remove(module)
