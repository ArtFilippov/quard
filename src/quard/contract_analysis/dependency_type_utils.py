"""Check class annotation boundaries using the discovered customer class index."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import cast

from quard._common import read_json
from quard.architecture_draft.generated_selection_utils import IDENTITY, MARKER
from quard.architecture_draft.group_area_utils import ORDINARY, area
from quard.contract_analysis.contract_utils import dotted, module_name
from quard.contract_analysis.contracts import Contracts
from quard.folder_analysis.generated_external_contracts import GeneratedOwnership

_SELF = frozenset({"Self", "typing.Self", "typing_extensions.Self"})
_CONCRETE = "CONCRETE_DEPENDENCY_TYPE"
_UNRESOLVED = "UNRESOLVED_DEPENDENCY_TYPE"
_SOURCE_ROOT = "src"
_MAIN = "main"
_ENVELOPES = "envelopes"
_PARENT = ".."


def _types(annotation: ast.expr) -> list[ast.expr]:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value, mode="eval")
        except SyntaxError:
            return [annotation]
        return [ast.copy_location(expression, annotation) for expression in _types(parsed.body)]
    if isinstance(annotation, (ast.Name, ast.Attribute)):
        return [annotation]
    return [item for child in ast.iter_child_nodes(annotation) if isinstance(child, ast.expr)
            for item in _types(child)]


def _method_annotations(item: ast.FunctionDef | ast.AsyncFunctionDef
                        ) -> list[tuple[ast.expr, str]]:
    results: list[tuple[ast.expr, str]] = []
    parameters = (*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs)
    parameters += tuple(arg for arg in (item.args.vararg, item.args.kwarg) if arg)
    for argument in parameters:
        if argument.annotation:
            results.append((argument.annotation, f"{item.name}.{argument.arg}"))
    if item.returns:
        results.append((item.returns, item.name))
    if (receiver := next(iter((*item.args.posonlyargs, *item.args.args)), None)) is None:
        return results
    for child in ast.walk(item):
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Attribute):
            target = child.target
            if isinstance(target.value, ast.Name) and target.value.id == receiver.arg:
                results.append((child.annotation, target.attr))
    return results


def _annotations(node: ast.ClassDef) -> list[tuple[ast.expr, str]]:
    results: list[tuple[ast.expr, str]] = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if dotted(item.annotation) not in {"ClassVar", "typing.ClassVar"}:
                results.append((item.annotation, item.target.id))
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            results.extend(_method_annotations(item))
    return results


def _read_trees(root: Path, paths: list[str]
                ) -> tuple[dict[str, ast.Module], list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in paths:
        if (Path(path).is_absolute() or _PARENT in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid dependency type path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "DEPENDENCY_TYPES_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path,
                                "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc)})
    return trees, diagnostics, error


def _classification(index: Contracts, resolved: str | None, path: str, *, grouped: bool
                     ) -> tuple[str, str] | None:
    if index.node_for(resolved) is not None:
        target = cast(str, resolved)
        if index.declaration(target) or (not grouped and
                                         module_name(index.path_for(target)) == module_name(path)):
            return None
        return _CONCRETE, ("a concrete class without a local contract" if grouped else
                           "a concrete class from another module")
    if resolved and index.local_module(resolved):
        return _UNRESOLVED, "an unresolved customer type"
    return None


def _violation(index: Contracts, owner: tuple[str, str], annotation: tuple[ast.expr, str],
               reference: ast.expr, *, grouped: bool) -> dict[str, object] | None:
    name, path = owner
    text = dotted(reference)
    if not text or text in _SELF:
        return None
    resolved = index.resolved(name, reference, path)
    if resolved in _SELF or resolved == name:
        return None
    if (classification := _classification(index, resolved, path, grouped=grouped)) is None:
        return None
    position = annotation[0] if isinstance(annotation[0], ast.Constant) else reference
    return {"rule": classification[0], "path": path, "line": position.lineno,
            "column": position.col_offset + 1,
            "symbol": f"{name.removeprefix(module_name(path) + '.')}.{annotation[1]}",
            "message": (f"{name}.{annotation[1]}: {text} resolves to {resolved}, "
                        + f"{classification[1]}; "
                        + "use a local contract.")}


def _class_diagnostics(index: Contracts, owner: tuple[str, ast.ClassDef, str], *, grouped: bool
                       ) -> list[dict[str, object]]:
    name, node, path = owner
    parts = Path(path).parts
    if parts[0] == _SOURCE_ROOT and (_MAIN in parts or parts[-2] == _ENVELOPES):
        return []
    results: list[dict[str, object]] = []
    for annotation in _annotations(node):
        for reference in _types(annotation[0]):
            if result := _violation(index, (name, path), annotation, reference, grouped=grouped):
                results.append(result)
    return results


def _function_diagnostics(index: Contracts, trees: dict[str, ast.Module]
                          ) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for path, tree in trees.items():
        if Path(path).parts[0] != _SOURCE_ROOT or area(path)[1] != ORDINARY:
            continue
        for function in tree.body:
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{module_name(path)}.{function.name}"
                annotations = _method_annotations(function)
            elif isinstance(function, ast.AnnAssign) and isinstance(function.target, ast.Name):
                name = module_name(path)
                annotations = [(function.annotation, function.target.id)]
            else:
                continue
            for annotation in annotations:
                for reference in _types(annotation[0]):
                    if result := _violation(index, (name, path), annotation, reference,
                                            grouped=True):
                        results.append(result)
    return results


def analyze(root: Path, paths: list[str]) -> tuple[list[dict[str, object]], str | None]:
    trees, diagnostics, error = _read_trees(root, paths)
    grouped = ((root / MARKER).exists() and read_json(root / MARKER) == IDENTITY)
    index = GeneratedOwnership(trees) if grouped else Contracts(trees)
    for owner in index.all_declarations():
        if grouped and Path(owner[2]).parts[0] != _SOURCE_ROOT:
            continue
        diagnostics.extend(_class_diagnostics(index, owner, grouped=grouped and
                                             Path(owner[2]).parts[0] == _SOURCE_ROOT and
                                              area(owner[2])[1] == ORDINARY))
    if grouped:
        diagnostics.extend(_function_diagnostics(index, trees))
    return diagnostics, error


def _manifest(path: Path) -> list[str]:
    paths = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(paths, list) or not all(isinstance(item, str)
                                              for item in cast(list[object], paths)):
        raise ValueError("Dependency type manifest must be a list of Python paths")
    return cast(list[str], paths)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check contract-only annotation boundaries.")
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        diagnostics, error = analyze(Path.cwd(), _manifest(args.manifest))
    except (OSError, ValueError, TypeError) as exc:
        error = "DEPENDENCY_TYPES_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc)}]
    print(json.dumps({"quard_dependency_types_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    raise SystemExit(main())
