"""Check eager syntax in discovered Python constructor bodies without executing source."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast

from eryx._constructor_origins import Origins, UNKNOWN, Environment, merge, module_name


RULE = "CONSTRUCTOR_PURITY"
PARSE_ERROR = "CONSTRUCTOR_PURITY_PARSE_ERROR"
EXECUTION_ERROR = "CONSTRUCTOR_PURITY_EXECUTION_ERROR"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def bindings(statements: list[ast.stmt]) -> set[str]:
    """Names local to a suite's scope, including conditional and later bindings."""
    names: set[str] = set()

    def collect(node: ast.AST) -> None:
        if isinstance(node, FUNCTIONS + (ast.ClassDef,)):
            names.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        if isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            collect(child)

    for statement in statements:
        collect(statement)
    return names


class ConstructorChecker:
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

    def reject(self, node: ast.expr | ast.stmt | ast.pattern, reason: str) -> None:
        self.diagnostics.append({"rule": RULE, "path": self.path,
                                 "line": node.lineno, "column": node.col_offset + 1,
                                 "symbol": self.symbol,
                                 "message": f"{self.symbol}: {reason}."})

    def origin(self, node: ast.expr) -> frozenset[str]:
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == self.receiver):
            return self.fields.get(node.attr, UNKNOWN)
        if isinstance(node, ast.Attribute) and self.origin(node.value) == {"typed-external"}:
            return frozenset({"external:member"})
        return self.origins.expression(node, self.env)

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
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            self.call(node.value, standalone=True)
        elif isinstance(node, FUNCTIONS):
            # Definition-time expressions are eager; the body is deferred.
            for decorator in node.decorator_list:
                self.expr(decorator)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.expr(default)
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                        node.args.vararg, node.args.kwarg):
                if arg is not None and arg.annotation is not None:
                    self.expr(arg.annotation)
            if node.returns is not None:
                self.expr(node.returns)
            self.env[node.name] = UNKNOWN
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


def analyze(root: Path, paths: list[object]) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error = None
    trees: dict[str, ast.Module] = {}
    for path in paths:
        if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix != ".py"):
            raise ValueError(f"Invalid constructor purity source path: {path!r}")
        try:
            tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = PARSE_ERROR
            diagnostics.append({"rule": error, "path": path,
                                "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
            continue
        trees[path] = tree
    origins = Origins(trees)
    for path, tree in trees.items():
        checker = ConstructorChecker(path, origins)

        def walk(node: ast.AST, owners: tuple[str, ...] = ()) -> None:
            if isinstance(node, ast.ClassDef):
                qualified = ".".join((*owners, node.name))
                for child in node.body:
                    if isinstance(child, FUNCTIONS) and child.name == "__init__":
                        checker.constructor(child, qualified, node)
                    walk(child, (*owners, node.name))
            elif isinstance(node, FUNCTIONS):
                for child in node.body:
                    walk(child, owners)
            else:
                for child in ast.iter_child_nodes(node):
                    walk(child, owners)

        walk(tree)
        diagnostics.extend(checker.diagnostics)
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("Constructor purity manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), cast(list[object], paths))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = EXECUTION_ERROR
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"eryx_constructor_purity_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
