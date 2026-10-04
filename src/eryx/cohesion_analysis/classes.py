"""Compute basic class cohesion from the consumer's discovered Python files."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import override

from eryx.cohesion_analysis.cohesion_analysis import CohesionAnalysis
from eryx.cohesion_analysis.declarations import Declarations
from eryx.cohesion_references.method_references import MethodReferences

# Python data-model hooks, rather than a blanket double-underscore pattern.
SPECIAL_METHODS = frozenset("""
__new__ __init__ __del__ __repr__ __str__ __bytes__ __format__ __lt__ __le__ __eq__
__ne__ __gt__ __ge__ __hash__ __bool__ __getattr__ __getattribute__ __setattr__
__delattr__ __dir__ __get__ __set__ __delete__ __set_name__ __slots__
__init_subclass__ __class_getitem__ __call__ __len__ __length_hint__ __getitem__
__setitem__ __delitem__ __missing__ __iter__ __next__ __reversed__ __contains__
__add__ __sub__ __mul__ __matmul__ __truediv__ __floordiv__ __mod__ __divmod__
__pow__ __lshift__ __rshift__ __and__ __xor__ __or__ __radd__ __rsub__ __rmul__
__rmatmul__ __rtruediv__ __rfloordiv__ __rmod__ __rdivmod__ __rpow__ __rlshift__
__rrshift__ __rand__ __rxor__ __ror__ __iadd__ __isub__ __imul__ __imatmul__
__itruediv__ __ifloordiv__ __imod__ __ipow__ __ilshift__ __irshift__ __iand__
__ixor__ __ior__ __neg__ __pos__ __abs__ __invert__ __complex__ __int__
__float__ __index__ __round__ __trunc__ __floor__ __ceil__ __enter__ __exit__
__aenter__ __aexit__ __await__ __aiter__ __anext__ __fspath__ __copy__
__deepcopy__ __reduce__ __reduce_ex__ __getstate__ __setstate__ __getnewargs__
__getnewargs_ex__ __sizeof__ __instancecheck__ __subclasscheck__ __mro_entries__
__prepare__ __post_init__ __buffer__ __release_buffer__
""".split())
METHOD = (ast.FunctionDef, ast.AsyncFunctionDef)
ABSTRACT_DECORATORS = frozenset({"abc.abstractmethod", "abc.abstractclassmethod",
                                 "abc.abstractstaticmethod", "abc.abstractproperty"})


def dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def is_placeholder(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = method.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
        if not body:
            return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return stmt.value.value is Ellipsis
    if isinstance(stmt, ast.Return):
        return stmt.value is None or isinstance(stmt.value, ast.Constant) and stmt.value.value is None
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        return (isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
                or isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                and exc.func.id == "NotImplementedError")
    return False


def eligible(method: ast.FunctionDef | ast.AsyncFunctionDef, declarations: Declarations) -> bool:
    return (method.name not in SPECIAL_METHODS
            and not declarations.decorated(method, "overload", "typing")
            and not declarations.decorated(method, "overload", "typing_extensions")
            and not declarations.is_abstract(method)
            and not is_placeholder(method))


def components(node: ast.ClassDef, declarations: Declarations) -> list[list[str]]:
    methods = [item for item in node.body if isinstance(item, METHOD) and eligible(item, declarations)]
    names = {item.name for item in methods}
    declared = {item.name for item in node.body if isinstance(item, METHOD)}
    references = []
    for method in methods:
        instance_receiver = None
        class_receivers = {node.name}
        arguments = method.args.posonlyargs + method.args.args
        static = any(isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
                     for decorator in method.decorator_list)
        classmethod = any(isinstance(decorator, ast.Name) and decorator.id == "classmethod"
                          for decorator in method.decorator_list)
        if arguments and not static:
            if classmethod:
                class_receivers.add(arguments[0].arg)
            else:
                instance_receiver = arguments[0].arg
        refs = MethodReferences(instance_receiver, class_receivers, names, declared)
        for statement in method.body:
            refs.visit(statement)
        references.append(refs)

    # Edges are undirected: a call or a shared field connects both methods.
    parents = list(range(len(methods)))

    def root(index: int) -> int:
        while parents[index] != index:
            index = parents[index]
        return index

    def connect(left: int, right: int) -> None:
        parents[root(left)] = root(right)

    for index, refs in enumerate(references):
        for other, method in enumerate(methods):
            if (index == other and refs.connects_to(refs, method.name)
                    or index != other and refs.connects_to(references[other], method.name)):
                connect(index, other)
    groups: dict[int, list[str]] = {}
    for index, method in enumerate(methods):
        groups.setdefault(root(index), []).append(method.name)
    return list(groups.values())


class Classes(ast.NodeVisitor, CohesionAnalysis):
    def __init__(self, path: str, declarations: Declarations):
        self.path = path
        self.declarations = declarations
        self.parents: list[str] = []
        self.diagnostics: list[dict[str, object]] = []

    @override
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = ".".join([*self.parents, node.name])
        if (self.declarations.protocol(symbol) or
                self.declarations.abc(symbol) and self.declarations.abstract_methods(symbol)):
            self.parents.append(node.name)
            self.generic_visit(node)
            self.parents.pop()
            return
        groups = components(node, self.declarations)
        score = len(groups)
        if score != 1:
            detail = ("no eligible methods" if score == 0 else
                      "disconnected method groups: " + "; ".join(", ".join(group) for group in groups))
            self.diagnostics.append({"rule": "LCOM4", "path": self.path, "line": node.lineno,
                                     "column": node.col_offset + 1, "symbol": symbol,
                                     "message": f"{symbol}: LCOM4 score {score}; {detail}.", "score": score,
                                     "groups": groups})
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    @override
    def results(self) -> list[dict[str, object]]:
        return self.diagnostics


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if not isinstance(path, str) or not path.endswith(".py") or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"Invalid LCOM4 source path: {path!r}")
        try:
            source = (root / path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "LCOM4_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
            continue
        visitor = Classes(path, Declarations(tree))
        visitor.visit(tree)
        diagnostics.extend(visitor.results())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(paths, list):
            raise ValueError("LCOM4 manifest must be a list of Python paths")
        diagnostics, error = analyze(Path.cwd(), paths)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        diagnostics, error = ([{"rule": "LCOM4_EXECUTION_ERROR", "message": str(exc),
                                "is_infrastructure_error": True}], "LCOM4_EXECUTION_ERROR")
    print(json.dumps({"eryx_lcom4_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
