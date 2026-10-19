"""AST visitor for first-party Python bindings and stored receiver fields."""

from __future__ import annotations

import ast

from quard.field_ownership.bindings import module_name


class BindingScanner(ast.NodeVisitor):
    """Report syntactic bindings, without treating external attribute access as ownership."""

    def __init__(self, path: str, lines: list[str], modules: frozenset[str],
                 overrides: frozenset[ast.expr]) -> None:
        self.path = path
        self.lines = lines
        self.diagnostics: list[dict[str, object]] = []
        self.in_class = False
        self.module = module_name(path)
        self.modules = modules
        self.overrides = overrides

    def local_module(self, name: str) -> bool:
        """Accept only a discovered module or its package prefix, never an external import."""
        return any(module == name or module.startswith(name + ".") for module in self.modules)

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
        previous = self.in_class
        self.in_class = True
        self.generic_visit(node)
        self.in_class = previous

    def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not (self.in_class and any(item in self.overrides for item in node.decorator_list)):
            self.add(node.name, node, locate=True)
        previous = self.in_class
        self.in_class = False
        self.generic_visit(node)
        self.in_class = previous

    visit_FunctionDef = function
    visit_AsyncFunctionDef = function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        previous = self.in_class
        self.in_class = False
        self.generic_visit(node)
        self.in_class = previous

    def visit_arg(self, node: ast.arg) -> None:
        self.add(node.arg, node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Param)):
            self.add(node.id, node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (self.in_class and any(isinstance(target, ast.Name) and target.id == "__slots__"
                                  for target in node.targets)):
            self.slots(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (self.in_class and isinstance(node.target, ast.Name)
                and node.target.id == "__slots__" and node.value is not None):
            self.slots(node.value)
        self.generic_visit(node)

    def slots(self, value: ast.expr) -> None:
        """Treat literal slot strings and mapping keys as declared attributes."""
        if isinstance(value, (ast.Tuple, ast.List)):
            items = value.elts
        elif isinstance(value, ast.Dict):
            items = [key for key in value.keys if key is not None]
        else:
            items = [value]
        for item in items:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                self.add(item.value, item)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.add(alias.asname, alias, locate=True)
            elif self.local_module(alias.name.split(".")[0]):
                # `import pkg.child` binds only `pkg`, whereas `as` binds the full alias.
                self.add(alias.name.split(".")[0], alias)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        package = self.module if self.path.endswith(("/__init__.py", "/__init__.pyi")) else (
            self.module.rpartition(".")[0])
        parts = package.split(".") if package else []
        if node.level:
            if node.level > len(parts):
                target = ""
            else:
                target = ".".join([*parts[:len(parts) - node.level + 1], node.module]
                                  if node.module else parts[:len(parts) - node.level + 1])
        else:
            target = node.module or ""
        for alias in node.names:
            if alias.asname:
                self.add(alias.asname, alias, locate=True)
            elif target and self.local_module(target):
                self.add(alias.name, alias)

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
