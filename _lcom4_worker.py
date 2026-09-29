"""Compute basic class cohesion from the consumer's discovered Python files."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys


class MethodReferences(ast.NodeVisitor):
    """References in one method body, excluding deferred nested scopes."""

    def __init__(self, receivers: set[str], methods: set[str]):
        self.receivers = receivers
        self.methods = methods
        self.calls: set[str] = set()
        self.fields: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id in self.receivers and node.func.attr in self.methods):
            self.calls.add(node.func.attr)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (isinstance(node.value, ast.Name) and node.value.id in self.receivers
                and node.attr not in self.methods):
            self.fields.add(node.attr)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        pass

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        pass

    def visit_Lambda(self, node: ast.Lambda) -> None:
        pass


def components(node: ast.ClassDef) -> list[list[str]]:
    methods = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
    names = {item.name for item in methods}
    references = []
    for method in methods:
        receivers = {node.name}
        if method.args.posonlyargs or method.args.args:
            if not any(isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
                       for decorator in method.decorator_list):
                receivers.add((method.args.posonlyargs + method.args.args)[0].arg)
        refs = MethodReferences(receivers, names)
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
            if method.name in refs.calls or (index != other and refs.fields & references[other].fields):
                connect(index, other)
    groups: dict[int, list[str]] = {}
    for index, method in enumerate(methods):
        groups.setdefault(root(index), []).append(method.name)
    return list(groups.values())


class Classes(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.parents: list[str] = []
        self.diagnostics: list[dict[str, object]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = ".".join([*self.parents, node.name])
        groups = components(node)
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
        visitor = Classes(path)
        visitor.visit(tree)
        diagnostics.extend(visitor.diagnostics)
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
