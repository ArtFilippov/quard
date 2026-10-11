"""AST visitor for first-party Python bindings and stored receiver fields."""

from __future__ import annotations

import ast


class BindingScanner(ast.NodeVisitor):
    """Report syntactic bindings, without treating external attribute access as ownership."""

    def __init__(self, path: str, lines: list[str]) -> None:
        self.path = path
        self.lines = lines
        self.diagnostics: list[dict[str, object]] = []
        self.receivers: list[str | None] = []
        self.in_class = False

    def add(self, name: str, node: ast.AST, *, locate: bool = False) -> None:
        from quard.leading_underscores_utils import forbidden
        if not forbidden(name):
            return
        line = getattr(node, "lineno", None)
        column = getattr(node, "col_offset", 0) + 1
        if locate and isinstance(line, int):
            found = self.lines[line - 1].encode("utf-8").find(name.encode("utf-8"), column - 1)
            if found >= 0:
                column = found + 1
        self.diagnostics.append({"rule": "LEADING_UNDERSCORE_NAME", "path": self.path,
                                 "line": line, "column": column, "symbol": name,
                                 "message": f"{name}: first-party binding starts with _."})

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.add(node.name, node, locate=True)
        self.receivers.append(None)
        previous = self.in_class
        self.in_class = True
        self.generic_visit(node)
        self.in_class = previous
        self.receivers.pop()

    def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.add(node.name, node, locate=True)
        receiver = None
        if self.in_class and node.args.posonlyargs + node.args.args:
            receiver = (node.args.posonlyargs + node.args.args)[0].arg
        self.receivers.append(receiver)
        previous = self.in_class
        self.in_class = False
        self.generic_visit(node)
        self.in_class = previous
        self.receivers.pop()

    visit_FunctionDef = function
    visit_AsyncFunctionDef = function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.receivers.append(None)
        previous = self.in_class
        self.in_class = False
        self.generic_visit(node)
        self.in_class = previous
        self.receivers.pop()

    def visit_arg(self, node: ast.arg) -> None:
        self.add(node.arg, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Param)):
            self.add(node.id, node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (isinstance(node.ctx, ast.Store) and self.receivers and self.receivers[-1]
                and isinstance(node.value, ast.Name) and node.value.id == self.receivers[-1]):
            self.add(node.attr, node, locate=True)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (self.in_class and any(isinstance(target, ast.Name) and target.id == "__slots__"
                                  for target in node.targets)
                and isinstance(node.value, (ast.Tuple, ast.List))):
            for item in node.value.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    self.add(item.value, item)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (self.receivers and self.receivers[-1] and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id == self.receivers[-1]
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
            self.add(node.args[1].value, node.args[1])
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.add(alias.asname, alias, locate=True)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.visit_Import(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.add(node.name, node, locate=True)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.add(node.name, node, locate=True)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.add(node.name, node, locate=True)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.add(node.rest, node, locate=True)
        self.generic_visit(node)

    def visit_TypeVar(self, node: ast.TypeVar) -> None:
        self.add(node.name, node, locate=True)
        self.generic_visit(node)

    def visit_ParamSpec(self, node: ast.ParamSpec) -> None:
        self.add(node.name, node, locate=True)
        self.generic_visit(node)

    def visit_TypeVarTuple(self, node: ast.TypeVarTuple) -> None:
        self.add(node.name, node, locate=True)
        self.generic_visit(node)
